from __future__ import annotations

import concurrent.futures
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig
from .discovery import PdfSource, discover_pdfs, source_fingerprint
from .state import JobRecord, RunLock, StateDB


@dataclass(frozen=True)
class WorkItem:
    source: PdfSource
    source_fingerprint: str
    relative_key: str
    output_pdf: Path
    output_text: Path
    may_replace_pdf: bool
    may_replace_text: bool


@dataclass(frozen=True)
class ItemResult:
    relative_key: str
    success: bool
    error: str | None = None
    elapsed_seconds: float = 0.0


@dataclass(frozen=True)
class TextQuality:
    alphanumeric_characters: int
    pages: int

    @property
    def alphanumeric_chars_per_page(self) -> float:
        return self.alphanumeric_characters / self.pages


@dataclass
class RunSummary:
    discovered: int = 0
    queued: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped_success: int = 0
    skipped_running: int = 0
    skipped_attempts: int = 0
    interrupted_recovered: int = 0
    discovery_errors: int = 0


class CommandCancelled(RuntimeError):
    """Raised inside a worker after the run is interrupted."""


class ProcessController:
    """Tracks active process groups so Ctrl-C can stop OCR children promptly."""

    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._processes: set[subprocess.Popen[str]] = set()

    def add(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._processes.add(process)
        if self.cancelled.is_set():
            self._terminate(process)

    def discard(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._processes.discard(process)

    def check(self) -> None:
        if self.cancelled.is_set():
            raise CommandCancelled("Interrupted by user")

    def cancel(self) -> None:
        self.cancelled.set()
        with self._lock:
            processes = list(self._processes)
        for process in processes:
            self._terminate(process)

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            try:
                result = subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    shell=False,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
                if result.returncode != 0 and process.poll() is None:
                    process.kill()
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _terminal(message: str) -> None:
    """Print progress without crashing on a legacy Windows console encoding."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    safe_message = message.encode(encoding, errors="backslashreplace").decode(encoding)
    print(safe_message)


def build_ocrmypdf_command(
    config: AppConfig,
    source: Path,
    output_pdf: Path,
    *,
    redo_ocr: bool = False,
) -> list[str]:
    ocr = config.ocr
    command = [
        *config.tools.ocrmypdf,
        "--language",
        ocr.languages,
        "--output-type",
        ocr.output_type,
        "--optimize",
        str(ocr.optimize),
        "--jobs",
        str(ocr.ocr_jobs),
    ]
    if ocr.rotate_pages:
        command.append("--rotate-pages")
    if ocr.deskew and not redo_ocr:
        command.append("--deskew")
    if redo_ocr:
        command.append("--redo-ocr")
    elif ocr.skip_text:
        command.append("--skip-text")
    command.extend(ocr.extra_args)
    command.extend((str(source), str(output_pdf)))
    return command


def build_pdftotext_command(config: AppConfig, source_pdf: Path, output_text: Path) -> list[str]:
    return [
        *config.tools.pdftotext,
        "-enc",
        "UTF-8",
        str(source_pdf),
        str(output_text),
    ]


def measure_text_quality(text_path: Path) -> TextQuality:
    text = text_path.read_text(encoding="utf-8")
    pages = text.count("\f")
    if not text.endswith("\f"):
        pages += 1
    return TextQuality(
        alphanumeric_characters=sum(character.isalnum() for character in text),
        pages=max(1, pages),
    )


def _run_command(
    command: list[str],
    timeout_seconds: int,
    logger: logging.Logger,
    controller: ProcessController,
) -> subprocess.CompletedProcess[str]:
    logger.info("Executing: %s", shlex.join(command))
    controller.check()
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=os.name != "nt",
        creationflags=creationflags,
    )
    controller.add(process)
    deadline = time.monotonic() + timeout_seconds if timeout_seconds else None
    try:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if controller.cancelled.is_set():
                    controller._terminate(process)
                    process.communicate()
                    raise CommandCancelled("Interrupted by user")
                if deadline is not None and time.monotonic() >= deadline:
                    controller._terminate(process)
                    process.communicate()
                    raise subprocess.TimeoutExpired(command, timeout_seconds)
    finally:
        controller.discard(process)
    controller.check()
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if result.stdout:
        logger.info("stdout:\n%s", result.stdout.rstrip())
    if result.stderr:
        logger.info("stderr:\n%s", result.stderr.rstrip())
    return result


def _copy_to_atomic_stage(
    source: Path, target: Path, controller: ProcessController
) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with source.open("rb") as incoming, stage.open("xb") as outgoing:
            while chunk := incoming.read(1024 * 1024):
                controller.check()
                outgoing.write(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        return stage
    except Exception:
        stage.unlink(missing_ok=True)
        raise


def _publish(stage: Path, target: Path, may_replace: bool) -> None:
    if may_replace:
        os.replace(stage, target)
        return
    try:
        # The stage is a sibling of the destination. A hard link gives us an
        # atomic create-if-absent operation, avoiding a check/replace race.
        os.link(stage, target)
    except FileExistsError as exc:
        raise RuntimeError(f"Refusing to overwrite unmanaged output: {target}") from exc
    except OSError as exc:
        raise RuntimeError(
            f"Could not atomically publish {target} without overwrite: {exc}"
        ) from exc
    try:
        stage.unlink()
    except OSError:
        # The target already points at the complete staged inode. Cleanup of a
        # leftover hidden stage must not turn a successful publish into failure.
        pass


def _publish_pair(
    pdf_stage: Path,
    text_stage: Path,
    item: WorkItem,
    controller: ProcessController,
) -> None:
    """Publish both artifacts as one rollback-capable transaction."""
    entries = (
        (pdf_stage, item.output_pdf, item.may_replace_pdf),
        (text_stage, item.output_text, item.may_replace_text),
    )
    backups: list[tuple[Path, Path]] = []
    published: list[Path] = []
    try:
        for _stage, target, may_replace in entries:
            if target.exists() and not may_replace:
                raise RuntimeError(f"Refusing to overwrite unmanaged output: {target}")
        for _stage, target, may_replace in entries:
            if target.exists() and may_replace:
                backup = target.with_name(f".{target.name}.{uuid.uuid4().hex}.bak")
                os.replace(target, backup)
                backups.append((target, backup))
        for stage, target, _may_replace in entries:
            controller.check()
            _publish(stage, target, False)
            published.append(target)
    except Exception as exc:
        rollback_errors: list[str] = []
        for target in reversed(published):
            try:
                target.unlink(missing_ok=True)
            except OSError as rollback_exc:
                rollback_errors.append(f"remove {target}: {rollback_exc}")
        for target, backup in reversed(backups):
            try:
                if backup.exists():
                    os.replace(backup, target)
            except OSError as rollback_exc:
                rollback_errors.append(f"restore {target}: {rollback_exc}")
        detail = f"; rollback errors: {'; '.join(rollback_errors)}" if rollback_errors else ""
        raise RuntimeError(f"Could not publish PDF/TXT pair: {exc}{detail}") from exc
    else:
        for _target, backup in backups:
            try:
                backup.unlink(missing_ok=True)
            except OSError:
                # The new pair is already durable; a leftover hidden backup is
                # safer than treating valid output as a failed generation.
                pass


def _ensure_safe_target(target: Path, expected_root: Path, input_root: Path) -> None:
    resolved = target.resolve(strict=False)
    if not resolved.is_relative_to(expected_root):
        raise RuntimeError(
            f"Output path escapes its configured root (possibly through a symlink): {target}"
        )
    if resolved.is_relative_to(input_root):
        raise RuntimeError(f"Refusing an output path inside the input tree: {target}")


def _create_and_extract(
    config: AppConfig,
    source: Path,
    output_pdf: Path,
    output_text: Path,
    logger: logging.Logger,
    controller: ProcessController,
    *,
    redo_ocr: bool = False,
) -> TextQuality:
    ocr_command = build_ocrmypdf_command(
        config, source, output_pdf, redo_ocr=redo_ocr
    )
    result = _run_command(
        ocr_command, config.ocr.timeout_seconds, logger, controller
    )
    mode = "OCRmyPDF --redo-ocr" if redo_ocr else "OCRmyPDF"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no diagnostic output").strip()
        raise RuntimeError(f"{mode} exited with code {result.returncode}: {detail[-4000:]}")
    if not output_pdf.is_file() or output_pdf.stat().st_size == 0:
        raise RuntimeError(f"{mode} did not create a non-empty PDF")

    check = _run_command(
        [*config.tools.qpdf, "--check", str(output_pdf)],
        config.ocr.timeout_seconds,
        logger,
        controller,
    )
    if check.returncode != 0:
        detail = (check.stderr or check.stdout or "no diagnostic output").strip()
        raise RuntimeError(f"qpdf validation failed after {mode}: {detail[-4000:]}")

    text_result = _run_command(
        build_pdftotext_command(config, output_pdf, output_text),
        config.ocr.timeout_seconds,
        logger,
        controller,
    )
    if text_result.returncode != 0:
        detail = (
            text_result.stderr or text_result.stdout or "no diagnostic output"
        ).strip()
        raise RuntimeError(
            f"pdftotext exited with code {text_result.returncode}: {detail[-4000:]}"
        )
    if not output_text.is_file():
        raise RuntimeError("pdftotext did not create the complete TXT output")
    return measure_text_quality(output_text)


def _process_item(
    item: WorkItem,
    config: AppConfig,
    logger: logging.Logger,
    controller: ProcessController,
) -> ItemResult:
    started = time.monotonic()
    job_dir = config.paths.work_dir / "jobs" / uuid.uuid4().hex
    job_dir.mkdir(parents=True, exist_ok=False)
    temporary_pdf = job_dir / "searchable.pdf"
    temporary_text = job_dir / "complete.txt"
    pdf_stage: Path | None = None
    text_stage: Path | None = None
    try:
        _ensure_safe_target(item.output_pdf, config.paths.searchable_dir, config.paths.input_dir)
        _ensure_safe_target(item.output_text, config.paths.text_dir, config.paths.input_dir)
        if item.output_pdf.exists() and not item.may_replace_pdf:
            raise RuntimeError(f"Refusing to overwrite unmanaged output: {item.output_pdf}")
        if item.output_text.exists() and not item.may_replace_text:
            raise RuntimeError(f"Refusing to overwrite unmanaged output: {item.output_text}")

        current = item.source.path.stat()
        if current.st_size != item.source.size or current.st_mtime_ns != item.source.mtime_ns:
            raise RuntimeError("Source changed after discovery; rerun to fingerprint the new file")

        quality = _create_and_extract(
            config,
            item.source.path,
            temporary_pdf,
            temporary_text,
            logger,
            controller,
        )
        minimum_quality = config.text_quality.minimum_alphanumeric_chars_per_page
        if (
            config.text_quality.automatic_redo
            and config.ocr.skip_text
            and quality.alphanumeric_chars_per_page < minimum_quality
        ):
            logger.warning(
                "Low text quality for %s: %d alphanumeric characters across %d pages "
                "(%.2f/page, minimum %d); retrying from the source with --redo-ocr",
                item.relative_key,
                quality.alphanumeric_characters,
                quality.pages,
                quality.alphanumeric_chars_per_page,
                minimum_quality,
            )
            _terminal(
                f"RETRY  {item.relative_key}: text quality "
                f"{quality.alphanumeric_chars_per_page:.2f}/page is below "
                f"{minimum_quality}; trying --redo-ocr"
            )
            redo_pdf = job_dir / "redo-searchable.pdf"
            redo_text = job_dir / "redo-complete.txt"
            redo_quality = _create_and_extract(
                config,
                item.source.path,
                redo_pdf,
                redo_text,
                logger,
                controller,
                redo_ocr=True,
            )
            if redo_quality.alphanumeric_chars_per_page < minimum_quality:
                raise RuntimeError(
                    "Text quality remained below the configured minimum after the "
                    f"automatic --redo-ocr retry: {redo_quality.alphanumeric_characters} "
                    f"alphanumeric characters across {redo_quality.pages} pages "
                    f"({redo_quality.alphanumeric_chars_per_page:.2f}/page; "
                    f"minimum {minimum_quality})"
                )
            logger.info(
                "Automatic --redo-ocr retry succeeded for %s with %.2f "
                "alphanumeric characters per page",
                item.relative_key,
                redo_quality.alphanumeric_chars_per_page,
            )
            _terminal(
                f"RETRY OK {item.relative_key}: --redo-ocr text quality "
                f"{redo_quality.alphanumeric_chars_per_page:.2f}/page"
            )
            temporary_pdf = redo_pdf
            temporary_text = redo_text

        # Stage beside each destination so each final rename is filesystem-atomic.
        pdf_stage = _copy_to_atomic_stage(temporary_pdf, item.output_pdf, controller)
        text_stage = _copy_to_atomic_stage(temporary_text, item.output_text, controller)
        _publish_pair(pdf_stage, text_stage, item, controller)
        pdf_stage = None
        text_stage = None
        elapsed = time.monotonic() - started
        logger.info("Completed %s in %.1fs", item.relative_key, elapsed)
        return ItemResult(item.relative_key, True, elapsed_seconds=elapsed)
    except CommandCancelled:
        error = "Interrupted by user"
    except subprocess.TimeoutExpired as exc:
        command = exc.cmd if isinstance(exc.cmd, (list, tuple)) else [exc.cmd]
        command_name = Path(str(command[0])).name if command and command[0] else "external command"
        error = f"{command_name} timed out after {exc.timeout} seconds"
    except (OSError, RuntimeError) as exc:
        error = str(exc)
    except Exception as exc:  # defensive boundary: one document must not stop the batch
        logger.exception("Unexpected failure for %s", item.relative_key)
        error = f"Unexpected {type(exc).__name__}: {exc}"
    finally:
        if text_stage is not None:
            text_stage.unlink(missing_ok=True)
        if pdf_stage is not None:
            pdf_stage.unlink(missing_ok=True)
        shutil.rmtree(job_dir, ignore_errors=True)
    elapsed = time.monotonic() - started
    logger.error("Failed %s after %.1fs: %s", item.relative_key, elapsed, error)
    return ItemResult(item.relative_key, False, error, elapsed)


def _owned(record: JobRecord | None, output_pdf: Path, output_text: Path) -> tuple[bool, bool]:
    if record is None:
        return False, False
    return (
        bool(record.owns_output_pdf and record.output_pdf == str(output_pdf)),
        bool(record.owns_output_text and record.output_text == str(output_text)),
    )


def run_batch(
    config: AppConfig,
    logger: logging.Logger,
    *,
    limit: int | None = None,
    retry_failed: bool = False,
    recover_lock: bool = False,
) -> RunSummary:
    paths = config.paths
    for directory in (
        paths.input_dir,
        paths.searchable_dir,
        paths.text_dir,
        paths.work_dir,
        paths.logs_dir,
        paths.state_db.parent,
        paths.failures_csv.parent,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    lock_path = paths.state_db.with_name(paths.state_db.name + ".run.lock")
    with RunLock(lock_path, recover=recover_lock):
        summary = RunSummary()
        walk_errors: list[OSError] = []
        sources = discover_pdfs(paths.input_dir, on_walk_error=walk_errors.append)
        if limit is not None:
            sources = sources[:limit]
        summary.discovered = len(sources)
        summary.discovery_errors = len(walk_errors)
        summary.failed += len(walk_errors)
        logger.info("Discovered %d PDF(s)", len(sources))
        for error in walk_errors:
            message = f"Could not scan directory {error.filename or paths.input_dir}: {error}"
            logger.error(message)
            _terminal(f"DISCOVERY FAILED: {message}")
        config_fingerprint = config.processing_fingerprint()
        work_items: list[WorkItem] = []

        with StateDB(paths.state_db) as state:
            summary.interrupted_recovered = state.recover_interrupted_running()
            if summary.interrupted_recovered:
                logger.warning(
                    "Recovered %d interrupted running job(s) under the exclusive lock",
                    summary.interrupted_recovered,
                )
            if retry_failed:
                reset = state.reset_failed()
                logger.info("Reset %d failed job(s) for retry", reset)

            for index, source in enumerate(sources, 1):
                relative_key = source.relative.as_posix()
                output_relative = source.relative.with_suffix(".pdf")
                text_relative = source.relative.with_suffix(".txt")
                output_pdf = paths.searchable_dir / output_relative
                output_text = paths.text_dir / text_relative
                prior = state.get(relative_key)
                may_replace_pdf, may_replace_text = _owned(
                    prior, output_pdf, output_text
                )
                logger.info("Hashing source %d/%d: %s", index, len(sources), relative_key)
                try:
                    if source.error:
                        raise OSError(source.error)
                    fingerprint = source_fingerprint(source.path)
                except OSError as exc:
                    fingerprint = f"unavailable:{source.size}:{source.mtime_ns}"
                    record = state.register(
                        relative_key,
                        source.path,
                        fingerprint,
                        config_fingerprint,
                        output_pdf,
                        output_text,
                    )
                    if record.status == "failed" and record.attempts >= config.ocr.max_attempts:
                        summary.skipped_attempts += 1
                        continue
                    error = f"Could not read source for fingerprinting: {type(exc).__name__}: {exc}"
                    state.mark_running(relative_key)
                    state.mark_failed(relative_key, error)
                    summary.queued += 1
                    summary.failed += 1
                    logger.error("Failed %s: %s", relative_key, error)
                    _terminal(f"PREPARE FAILED {relative_key}: {error}")
                    continue

                record = state.register(
                    relative_key,
                    source.path,
                    fingerprint,
                    config_fingerprint,
                    output_pdf,
                    output_text,
                )
                matching = (
                    record.source_fingerprint == fingerprint
                    and record.config_fingerprint == config_fingerprint
                )
                if matching and record.status == "success":
                    if output_pdf.is_file() and output_text.is_file():
                        summary.skipped_success += 1
                        continue
                    state.mark_pending(relative_key, "A managed output was missing; queued again")
                elif matching and record.status == "running":
                    summary.skipped_running += 1
                    continue
                elif (
                    matching
                    and record.status == "failed"
                    and record.attempts >= config.ocr.max_attempts
                ):
                    summary.skipped_attempts += 1
                    continue

                work_items.append(
                    WorkItem(
                        source,
                        fingerprint,
                        relative_key,
                        output_pdf,
                        output_text,
                        may_replace_pdf,
                        may_replace_text,
                    )
                )

            summary.queued += len(work_items)
            total = len(work_items)
            if not total:
                state.export_failures(paths.failures_csv)
                return summary

            controller = ProcessController()
            executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=config.ocr.document_workers,
                thread_name_prefix="ocr-document",
            )
            active: dict[concurrent.futures.Future[ItemResult], WorkItem] = {}
            remaining = iter(work_items)

            def fill_worker_slots() -> None:
                while len(active) < config.ocr.document_workers:
                    try:
                        item = next(remaining)
                    except StopIteration:
                        return
                    state.mark_running(item.relative_key)
                    try:
                        future = executor.submit(
                            _process_item, item, config, logger, controller
                        )
                    except Exception:
                        state.unclaim(item.relative_key, "Worker submission failed")
                        raise
                    active[future] = item

            completed = 0
            try:
                fill_worker_slots()
                while active:
                    done, _not_done = concurrent.futures.wait(
                        tuple(active),
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                    for future in done:
                        completed += 1
                        item = active.pop(future)
                        try:
                            result = future.result()
                        except (KeyboardInterrupt, SystemExit):
                            state.mark_pending(
                                item.relative_key, "Interrupted by user; retry next run"
                            )
                            raise
                        except Exception as exc:  # defensive worker boundary
                            result = ItemResult(
                                item.relative_key,
                                False,
                                f"Worker crashed with {type(exc).__name__}: {exc}",
                            )
                        if result.success:
                            state.mark_success(result.relative_key)
                            summary.succeeded += 1
                            _terminal(
                                f"[{completed}/{total}] OK     {result.relative_key} "
                                f"({result.elapsed_seconds:.1f}s)"
                            )
                        else:
                            state.mark_failed(
                                result.relative_key, result.error or "Unknown failure"
                            )
                            summary.failed += 1
                            _terminal(
                                f"[{completed}/{total}] FAILED "
                                f"{result.relative_key}: {result.error}"
                            )
                    fill_worker_slots()
            except BaseException:
                controller.cancel()
                for future, item in active.items():
                    if future.cancel():
                        state.unclaim(item.relative_key, "Cancelled before worker start")
                    else:
                        state.mark_pending(item.relative_key, "Interrupted by user; retry next run")
                executor.shutdown(wait=True, cancel_futures=True)
                state.export_failures(paths.failures_csv)
                raise
            else:
                executor.shutdown(wait=True, cancel_futures=True)
            state.export_failures(paths.failures_csv)
        return summary

