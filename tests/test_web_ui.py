from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arabic_ocr_batch.web_ui import (
    choose_llama_server,
    contiguous_ranges,
    create_review_report,
    parse_page_selection,
    safe_output_name,
)


def make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


class LlamaBackendSelectionTests(unittest.TestCase):
    def test_explicit_server_override_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            override = make_executable(root / "custom" / "llama-server")
            make_executable(root / "llama" / "llama-server")
            make_executable(root / "llama-cuda" / "llama-server")

            selected, backend = choose_llama_server(
                engine_root=root,
                environment={"ARABIC_OCR_LLAMA_SERVER": str(override)},
                cuda_probe=lambda _: True,
            )

        self.assertEqual(selected, override)
        self.assertEqual(backend, "custom")

    def test_usable_cuda_server_is_preferred(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_executable(root / "llama" / "llama-server")
            cuda = make_executable(root / "llama-cuda" / "llama-server")

            selected, backend = choose_llama_server(
                engine_root=root,
                environment={},
                cuda_probe=lambda path: path == cuda,
            )

        self.assertEqual(selected, cuda)
        self.assertEqual(backend, "cuda")

    def test_failed_cuda_probe_falls_back_to_cpu(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cpu = make_executable(root / "llama" / "llama-server")
            make_executable(root / "llama-cuda" / "llama-server")

            selected, backend = choose_llama_server(
                engine_root=root,
                environment={},
                cuda_probe=lambda _: False,
            )

        self.assertEqual(selected, cpu)
        self.assertEqual(backend, "cpu")


class PageSelectionTests(unittest.TestCase):
    def test_all_pages(self) -> None:
        self.assertEqual(parse_page_selection("all", 4), [1, 2, 3, 4])

    def test_ranges_are_sorted_and_deduplicated(self) -> None:
        self.assertEqual(parse_page_selection("5, 1-3, 3", 6), [1, 2, 3, 5])

    def test_out_of_bounds_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside"):
            parse_page_selection("1-8", 7)

    def test_contiguous_ranges(self) -> None:
        self.assertEqual(contiguous_ranges([1, 2, 4, 7, 8]), [(1, 2), (4, 4), (7, 8)])


class SafetyTests(unittest.TestCase):
    def test_output_name_removes_path_characters(self) -> None:
        self.assertEqual(safe_output_name('../bad:name?.pdf'), '.._bad_name_.pdf')


class ConfidenceReportTests(unittest.TestCase):
    def test_report_marks_only_reviewable_low_confidence_tokens(self) -> None:
        xml = """<?xml version='1.0' encoding='UTF-8'?>
        <alto xmlns='http://www.loc.gov/standards/alto/ns-v4#'>
          <Layout><Page WIDTH='1000'><PrintSpace><TextBlock>
            <TextLine HPOS='700' VPOS='100' WIDTH='100' HEIGHT='30'>
              <String CONTENT='الحلاق' WC='0.55'/><String CONTENT=':' WC='0.20'/>
            </TextLine>
            <TextLine HPOS='200' VPOS='100' WIDTH='450' HEIGHT='30'>
              <String CONTENT='مرحبا' WC='0.75'/><String CONTENT='بكم' WC='0.95'/>
            </TextLine>
          </TextBlock></PrintSpace></Page></Layout>
        </alto>"""
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / 'page-0001.alto.xml').write_text(xml, encoding='utf-8')
            report = create_review_report(directory, directory / 'review.json')
        self.assertEqual(
            report['summary'],
            {'pages': 1, 'yellow': 1, 'red': 1, 'below_96': 3, 'suspicious': 0},
        )
        self.assertTrue(report['pages'][0]['rows'][0]['speaker_like'])

    def test_page_number_header_is_not_counted_as_uncertain(self) -> None:
        xml = """<?xml version='1.0' encoding='UTF-8'?>
        <alto xmlns='http://www.loc.gov/standards/alto/ns-v4#'>
          <Layout><Page WIDTH='1000' HEIGHT='1600'><PrintSpace><TextBlock>
            <TextLine HPOS='450' VPOS='80' WIDTH='100' HEIGHT='30'>
              <String CONTENT='-١٧٢-' WC='0.20'/>
            </TextLine>
            <TextLine HPOS='200' VPOS='500' WIDTH='500' HEIGHT='30'>
              <String CONTENT='كلمة' WC='0.70'/>
            </TextLine>
          </TextBlock></PrintSpace></Page></Layout>
        </alto>"""
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / 'page-0001.alto.xml').write_text(xml, encoding='utf-8')
            report = create_review_report(directory, directory / 'review.json')
        self.assertEqual(
            report['summary'],
            {'pages': 1, 'yellow': 1, 'red': 0, 'below_96': 1, 'suspicious': 0},
        )


if __name__ == "__main__":
    unittest.main()
