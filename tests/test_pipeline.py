from __future__ import annotations

import json
import logging
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from arabic_ocr_batch.config import load_config
from arabic_ocr_batch.discovery import source_fingerprint
from arabic_ocr_batch import pipeline
from arabic_ocr_batch.pipeline import (
    CommandCancelled,
    ProcessController,
    _run_command,
    build_ocrmypdf_command,
    build_pdftotext_command,
    run_batch,
)
from arabic_ocr_batch.state import RunLock, RunLockError, StateDB


FAKE_TOOL = r'''
from pathlib import Path
import sys
import time

mode = sys.argv[1]
args = sys.argv[2:]
if mode == "ocr-fail":
    print("simulated OCR failure", file=sys.stderr)
    raise SystemExit(7)
if mode == "sleep":
    time.sleep(30)
    raise SystemExit(0)
if mode == "ocr":
    source = Path(args[-2])
    output = Path(args[-1])
    redo_marker = b"REDO-OCR\n" if "--redo-ocr" in args else b""
    output.write_bytes(b"%PDF-1.4\n" + redo_marker + source.read_bytes())
    raise SystemExit(0)
if mode == "qpdf":
    candidate = Path(args[-1])
    raise SystemExit(0 if candidate.read_bytes().startswith(b"%PDF-") else 3)
if mode == "pdftotext-fail":
    print("simulated text extraction failure", file=sys.stderr)
    raise SystemExit(8)
if mode == "pdftotext-no-output":
    raise SystemExit(0)
if mode == "pdftotext":
    candidate = Path(args[-2])
    output = Path(args[-1])
    contents = candidate.read_bytes()
    if b"always-bad" in contents:
        text = "ss\fss\f"
    elif b"bogus-layer" in contents and b"REDO-OCR" not in contents:
        text = "ss\fss\f"
    elif b"bogus-layer" in contents:
        text = "هذا نص عربي واضح بعد إعادة التعرف الضوئي\f"
    elif b"existing-text" in contents:
        text = "نص موجود أصلا\n"
    else:
        text = "مرحبا بالعالم\n"
    output.write_text(text, encoding="utf-8")
    raise SystemExit(0)
raise SystemExit(9)
'''


def make_project(
    root: Path, ocr_mode: str = "ocr", pdftotext_mode: str = "pdftotext"
):
    fake = root / "fake_tool.py"
    fake.write_text(FAKE_TOOL, encoding="utf-8")
    python = sys.executable
    config_text = f'''
[paths]
input_dir = "input"
searchable_dir = "output/searchable"
text_dir = "output/text"
work_dir = "work"
logs_dir = "logs"
state_db = "state/state.sqlite3"
failures_csv = "reports/failures.csv"

[ocr]
languages = "ara+eng"
output_type = "pdfa"
rotate_pages = true
deskew = true
skip_text = true
optimize = 1
ocr_jobs = 2
document_workers = 1
max_attempts = 2
timeout_seconds = 30

[runtime]
minimum_free_gb = 0

[tools]
ocrmypdf = {json.dumps([python, str(fake), ocr_mode])}
tesseract = {json.dumps([python, str(fake), "tesseract"])}
qpdf = {json.dumps([python, str(fake), "qpdf"])}
pdftotext = {json.dumps([python, str(fake), pdftotext_mode])}
ghostscript = {json.dumps([python, str(fake), "ghostscript"])}
pdfinfo = {json.dumps([python, str(fake), "pdfinfo"])}
'''
    config_path = root / "config.toml"
    config_path.write_text(config_text, encoding="utf-8")
    (root / "input").mkdir(exist_ok=True)
    logger = logging.getLogger(f"test-{root.name}-{ocr_mode}-{pdftotext_mode}")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return load_config(config_path), logger


class PipelineTests(unittest.TestCase):
    def test_build_command_contains_safe_defaults_and_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config, _ = make_project(Path(temporary))
            command = build_ocrmypdf_command(
                config, Path("source.pdf"), Path("result.pdf")
            )
            self.assertIn("--rotate-pages", command)
            self.assertIn("--deskew", command)
            self.assertIn("--skip-text", command)
            self.assertEqual(command[command.index("--jobs") + 1], "2")
            self.assertEqual(command[command.index("--output-type") + 1], "pdfa")
            self.assertNotIn("--sidecar", command)
            self.assertEqual(command[-2:], ["source.pdf", "result.pdf"])
            redo_command = build_ocrmypdf_command(
                config, Path("source.pdf"), Path("redo.pdf"), redo_ocr=True
            )
            self.assertIn("--redo-ocr", redo_command)
            self.assertNotIn("--skip-text", redo_command)
            self.assertNotIn("--deskew", redo_command)
            self.assertIn("--rotate-pages", redo_command)
            self.assertEqual(redo_command[-2:], ["source.pdf", "redo.pdf"])
            text_command = build_pdftotext_command(
                config, Path("result.pdf"), Path("complete.txt")
            )
            self.assertEqual(text_command[-4:], ["-enc", "UTF-8", "result.pdf", "complete.txt"])

    def test_fake_success_nested_unicode_and_restart_skip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, logger = make_project(root)
            source = root / "input" / "دفعة" / "اختبار.pdf"
            source.parent.mkdir()
            source.write_bytes(b"scanned-page")

            first = run_batch(config, logger)
            self.assertEqual(first.succeeded, 1)
            self.assertEqual(first.failed, 0)
            self.assertTrue((root / "output/searchable/دفعة/اختبار.pdf").is_file())
            self.assertEqual(
                (root / "output/text/دفعة/اختبار.txt").read_text(encoding="utf-8"),
                "مرحبا بالعالم\n",
            )

            second = run_batch(config, logger)
            self.assertEqual(second.queued, 0)
            self.assertEqual(second.skipped_success, 1)
            with StateDB(config.paths.state_db) as state:
                record = state.get("دفعة/اختبار.pdf")
                self.assertIsNotNone(record)
                self.assertEqual(record.attempts, 1)

    def test_existing_pdf_text_is_included_in_complete_txt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, logger = make_project(root)
            (root / "input" / "existing.pdf").write_bytes(b"existing-text")

            summary = run_batch(config, logger)

            self.assertEqual(summary.succeeded, 1)
            output = (root / "output/text/existing.txt").read_text(encoding="utf-8")
            self.assertEqual(output, "نص موجود أصلا\n")
            self.assertNotIn("OCR skipped", output)

    def test_adequate_text_does_not_trigger_redo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, logger = make_project(root)
            (root / "input" / "adequate.pdf").write_bytes(b"adequate")

            summary = run_batch(config, logger)

            self.assertEqual(summary.succeeded, 1)
            output_pdf = (root / "output/searchable/adequate.pdf").read_bytes()
            self.assertNotIn(b"REDO-OCR", output_pdf)

    def test_bogus_existing_text_triggers_redo_and_publishes_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, logger = make_project(root)
            (root / "input" / "bogus.pdf").write_bytes(b"bogus-layer")

            summary = run_batch(config, logger)

            self.assertEqual(summary.succeeded, 1)
            self.assertEqual(summary.failed, 0)
            output_pdf = (root / "output/searchable/bogus.pdf").read_bytes()
            output_text = (root / "output/text/bogus.txt").read_text(encoding="utf-8")
            self.assertIn(b"REDO-OCR", output_pdf)
            self.assertIn("هذا نص عربي واضح", output_text)

    def test_inadequate_redo_fails_without_publishing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, logger = make_project(root)
            (root / "input" / "bad-layer.pdf").write_bytes(b"always-bad")

            summary = run_batch(config, logger)

            self.assertEqual(summary.failed, 1)
            self.assertEqual(summary.succeeded, 0)
            self.assertFalse((root / "output/searchable/bad-layer.pdf").exists())
            self.assertFalse((root / "output/text/bad-layer.txt").exists())
            failures = config.paths.failures_csv.read_text(encoding="utf-8-sig")
            self.assertIn("2.00/page", failures)
            self.assertIn("minimum 10", failures)

    def test_failure_is_recorded_and_exported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, logger = make_project(root, "ocr-fail")
            (root / "input" / "bad.pdf").write_bytes(b"bad")
            summary = run_batch(config, logger)
            self.assertEqual(summary.failed, 1)
            csv_text = config.paths.failures_csv.read_text(encoding="utf-8-sig")
            self.assertIn("bad.pdf", csv_text)
            self.assertIn("simulated OCR failure", csv_text)

    def test_interrupted_running_job_is_recovered_under_lock_and_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, logger = make_project(root)
            source = root / "input" / "stale.pdf"
            source.write_bytes(b"stale")
            output_pdf = root / "output/searchable/stale.pdf"
            output_text = root / "output/text/stale.txt"
            with StateDB(config.paths.state_db) as state:
                state.register(
                    "stale.pdf",
                    source,
                    source_fingerprint(source),
                    config.processing_fingerprint(),
                    output_pdf,
                    output_text,
                )
                state.mark_running("stale.pdf")
            summary = run_batch(config, logger)
            self.assertEqual(summary.interrupted_recovered, 1)
            self.assertEqual(summary.succeeded, 1)

    def test_refuses_unmanaged_output_without_overwriting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, logger = make_project(root)
            (root / "input" / "collision.pdf").write_bytes(b"source")
            existing = root / "output/searchable/collision.pdf"
            existing.parent.mkdir(parents=True)
            existing.write_bytes(b"user-owned")
            summary = run_batch(config, logger)
            self.assertEqual(summary.failed, 1)
            self.assertEqual(existing.read_bytes(), b"user-owned")
            self.assertFalse((root / "output/text/collision.txt").exists())

    def test_operational_settings_do_not_invalidate_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, logger = make_project(root)
            (root / "input" / "stable.pdf").write_bytes(b"stable")
            first = run_batch(config, logger)
            self.assertEqual(first.succeeded, 1)
            operational_change = replace(
                config,
                ocr=replace(
                    config.ocr,
                    document_workers=3,
                    ocr_jobs=7,
                    max_attempts=9,
                    timeout_seconds=1,
                ),
            )
            self.assertEqual(
                config.processing_fingerprint(),
                operational_change.processing_fingerprint(),
            )
            text_command_change = replace(
                config,
                tools=replace(config.tools, pdftotext=("different-pdftotext",)),
            )
            self.assertNotEqual(
                config.processing_fingerprint(),
                text_command_change.processing_fingerprint(),
            )
            quality_change = replace(
                config,
                text_quality=replace(
                    config.text_quality,
                    minimum_alphanumeric_chars_per_page=20,
                ),
            )
            self.assertNotEqual(
                config.processing_fingerprint(), quality_change.processing_fingerprint()
            )
            second = run_batch(operational_change, logger)
            self.assertEqual(second.skipped_success, 1)
            self.assertEqual(second.queued, 0)

    def test_publish_pair_failure_rolls_back_and_can_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, logger = make_project(root)
            source = root / "input" / "generation.pdf"
            source.write_bytes(b"generation-one")
            self.assertEqual(run_batch(config, logger).succeeded, 1)
            output_pdf = root / "output/searchable/generation.pdf"
            output_text = root / "output/text/generation.txt"
            old_pdf = output_pdf.read_bytes()
            old_text = output_text.read_bytes()

            source.write_bytes(b"generation-two")
            real_publish = pipeline._publish
            publish_calls = 0

            def fail_second_publish(stage: Path, target: Path, may_replace: bool) -> None:
                nonlocal publish_calls
                publish_calls += 1
                if publish_calls == 2:
                    raise RuntimeError("injected second-artifact failure")
                real_publish(stage, target, may_replace)

            with mock.patch.object(pipeline, "_publish", side_effect=fail_second_publish):
                failed = run_batch(config, logger)
            self.assertEqual(failed.failed, 1)
            self.assertEqual(output_pdf.read_bytes(), old_pdf)
            self.assertEqual(output_text.read_bytes(), old_text)

            retried = run_batch(config, logger)
            self.assertEqual(retried.succeeded, 1)
            self.assertIn(b"generation-two", output_pdf.read_bytes())

    def test_pdftotext_failure_preserves_previous_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, logger = make_project(root)
            source = root / "input" / "generation.pdf"
            source.write_bytes(b"generation-one")
            self.assertEqual(run_batch(config, logger).succeeded, 1)
            output_pdf = root / "output/searchable/generation.pdf"
            output_text = root / "output/text/generation.txt"
            old_pdf = output_pdf.read_bytes()
            old_text = output_text.read_bytes()

            source.write_bytes(b"existing-text-generation-two")
            failing_config, _ = make_project(root, pdftotext_mode="pdftotext-fail")
            failed = run_batch(failing_config, logger)

            self.assertEqual(failed.failed, 1)
            self.assertEqual(output_pdf.read_bytes(), old_pdf)
            self.assertEqual(output_text.read_bytes(), old_text)

    def test_missing_pdftotext_output_fails_without_publishing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, logger = make_project(root, pdftotext_mode="pdftotext-no-output")
            (root / "input" / "missing-text.pdf").write_bytes(b"scanned-page")

            failed = run_batch(config, logger)

            self.assertEqual(failed.failed, 1)
            self.assertFalse((root / "output/searchable/missing-text.pdf").exists())
            self.assertFalse((root / "output/text/missing-text.txt").exists())

    def test_legacy_sidecar_success_reprocesses_once_then_skips(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, logger = make_project(root)
            source = root / "input" / "legacy.pdf"
            source.write_bytes(b"existing-text")
            output_pdf = root / "output/searchable/legacy.pdf"
            output_text = root / "output/text/legacy.txt"
            output_pdf.parent.mkdir(parents=True)
            output_text.parent.mkdir(parents=True)
            output_pdf.write_bytes(b"%PDF-1.4\nlegacy-generation")
            output_text.write_text("[OCR skipped on page(s) 1-10]\n", encoding="utf-8")
            with StateDB(config.paths.state_db) as state:
                state.register(
                    "legacy.pdf",
                    source,
                    source_fingerprint(source),
                    "legacy-sidecar-fingerprint",
                    output_pdf,
                    output_text,
                )
                state.mark_running("legacy.pdf")
                state.mark_success("legacy.pdf")

            migrated = run_batch(config, logger)
            self.assertEqual(migrated.succeeded, 1)
            self.assertEqual(output_text.read_text(encoding="utf-8"), "نص موجود أصلا\n")

            next_run = run_batch(config, logger)
            self.assertEqual(next_run.queued, 0)
            self.assertEqual(next_run.skipped_success, 1)

    def test_hash_failure_is_isolated_and_exported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, logger = make_project(root)
            good = root / "input" / "good.pdf"
            bad = root / "input" / "unreadable.pdf"
            vanished = root / "input" / "vanished.pdf"
            good.write_bytes(b"good")
            bad.write_bytes(b"bad")
            vanished.write_bytes(b"vanished")
            real_fingerprint = pipeline.source_fingerprint

            def selective_fingerprint(path: Path) -> str:
                if path.name == "unreadable.pdf":
                    raise PermissionError("simulated unreadable source")
                if path.name == "vanished.pdf":
                    path.unlink()
                return real_fingerprint(path)

            with mock.patch.object(
                pipeline, "source_fingerprint", side_effect=selective_fingerprint
            ):
                summary = run_batch(config, logger)
            self.assertEqual(summary.succeeded, 1)
            self.assertEqual(summary.failed, 2)
            failures = config.paths.failures_csv.read_text(encoding="utf-8-sig")
            self.assertIn("unreadable.pdf", failures)
            self.assertIn("simulated unreadable source", failures)
            self.assertIn("vanished.pdf", failures)
            self.assertIn("FileNotFoundError", failures)

    def test_run_lock_is_exclusive_and_released(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "state" / "run.lock"
            with RunLock(lock_path):
                with self.assertRaises(RunLockError):
                    RunLock(lock_path).acquire()
                with self.assertRaisesRegex(RunLockError, "live run lock"):
                    RunLock(lock_path, recover=True).acquire()
            with RunLock(lock_path):
                self.assertTrue(lock_path.exists())
            self.assertFalse(lock_path.exists())

    def test_interruption_does_not_claim_unstarted_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, logger = make_project(root)
            for name in ("one.pdf", "two.pdf", "three.pdf"):
                (root / "input" / name).write_bytes(name.encode())
            with mock.patch.object(
                pipeline, "_process_item", side_effect=KeyboardInterrupt
            ):
                with self.assertRaises(KeyboardInterrupt):
                    run_batch(config, logger)
            with StateDB(config.paths.state_db) as state:
                records = [state.get(name) for name in ("one.pdf", "two.pdf", "three.pdf")]
            self.assertEqual(records[0].attempts, 1)
            self.assertEqual(records[0].status, "pending")
            self.assertEqual([record.attempts for record in records[1:]], [0, 0])
            self.assertEqual([record.status for record in records[1:]], ["pending", "pending"])

    def test_process_controller_stops_a_long_running_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, logger = make_project(root, "sleep")
            controller = ProcessController()
            errors: list[BaseException] = []

            def invoke() -> None:
                try:
                    _run_command(
                        list(config.tools.ocrmypdf), 0, logger, controller
                    )
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(target=invoke)
            started = time.monotonic()
            thread.start()
            time.sleep(0.4)
            controller.cancel()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertLess(time.monotonic() - started, 5)
            self.assertTrue(any(isinstance(error, CommandCancelled) for error in errors))


if __name__ == "__main__":
    unittest.main()

