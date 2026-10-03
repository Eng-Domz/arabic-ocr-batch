from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from arabic_ocr_batch.config import ConfigError, load_config
from arabic_ocr_batch import discovery
from arabic_ocr_batch.discovery import discover_pdfs, source_fingerprint


BASE_CONFIG = """
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
document_workers = 1
ocr_jobs = 2
"""


class ConfigAndDiscoveryTests(unittest.TestCase):
    def test_loads_relative_paths_from_config_location(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.toml"
            config_path.write_text(BASE_CONFIG, encoding="utf-8")
            config = load_config(config_path)
            self.assertEqual(config.paths.input_dir, (root / "input").resolve())
            self.assertEqual(config.ocr.languages, "ara+eng")
            self.assertEqual(config.ocr.ocr_jobs, 2)
            self.assertEqual(config.ocr.document_workers, 1)
            self.assertTrue(config.text_quality.automatic_redo)
            self.assertEqual(
                config.text_quality.minimum_alphanumeric_chars_per_page, 10
            )

    def test_rejects_invalid_text_quality_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.toml"
            config_path.write_text(
                BASE_CONFIG
                + """
[text_quality]
automatic_redo = "yes"
minimum_alphanumeric_chars_per_page = 0
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ConfigError, "text_quality.automatic_redo must be true or false"
            ):
                load_config(config_path)

            config_path.write_text(
                BASE_CONFIG
                + """
[text_quality]
automatic_redo = true
minimum_alphanumeric_chars_per_page = 0
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ConfigError,
                "text_quality.minimum_alphanumeric_chars_per_page must be >= 1",
            ):
                load_config(config_path)

    def test_rejects_output_nested_inside_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.toml"
            config_path.write_text(
                BASE_CONFIG.replace(
                    'searchable_dir = "output/searchable"',
                    'searchable_dir = "input/generated"',
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "overlapping"):
                load_config(config_path)

    def test_discovers_nested_unicode_pdfs_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "مجموعة" / "فرعي"
            nested.mkdir(parents=True)
            pdf = nested / "وثيقة عربية.PDF"
            pdf.write_bytes(b"sample-pdf")
            (nested / "notes.txt").write_text("ignore", encoding="utf-8")
            results = discover_pdfs(root)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].relative.as_posix(), "مجموعة/فرعي/وثيقة عربية.PDF")
            self.assertEqual(
                source_fingerprint(pdf),
                "ec64ca2459288e726d8d4184536cdb772c053695b9cf05efdb9a9eb8d9b0e42f",
            )

    def test_stat_error_is_attached_to_one_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            good = root / "good.pdf"
            broken = root / "broken.pdf"
            good.write_bytes(b"good")
            broken.write_bytes(b"broken")
            real_stat = Path.stat

            def selective_stat(path: Path, *args, **kwargs):
                if path == broken:
                    raise PermissionError("simulated stat denial")
                return real_stat(path, *args, **kwargs)

            with mock.patch.object(Path, "stat", selective_stat):
                results = discover_pdfs(root)
            by_name = {item.path.name: item for item in results}
            self.assertIsNone(by_name["good.pdf"].error)
            self.assertIn("simulated stat denial", by_name["broken.pdf"].error)

    def test_walk_error_can_be_reported_while_accessible_files_continue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "good.pdf").write_bytes(b"good")
            real_walk = discovery.os.walk

            def walk_with_warning(*args, onerror=None, **kwargs):
                if onerror is not None:
                    error = PermissionError("simulated directory denial")
                    error.filename = str(root / "blocked")
                    onerror(error)
                yield from real_walk(*args, onerror=onerror, **kwargs)

            errors: list[OSError] = []
            with mock.patch.object(discovery.os, "walk", side_effect=walk_with_warning):
                results = discover_pdfs(root, on_walk_error=errors.append)
            self.assertEqual([item.path.name for item in results], ["good.pdf"])
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], PermissionError)
            self.assertEqual(errors[0].filename, str(root / "blocked"))


if __name__ == "__main__":
    unittest.main()

