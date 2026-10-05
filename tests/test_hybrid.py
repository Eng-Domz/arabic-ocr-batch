from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from arabic_ocr_batch.hybrid import (
    create_hybrid_report,
    load_surya_results,
    normalize_word,
    remap_surya_pages,
    select_smart_pages,
    write_hybrid_text,
)


def pp_page(number: int, words: list[tuple[str, float]]) -> dict[str, object]:
    return {
        "number": number,
        "mean_confidence": sum(confidence for _, confidence in words) / len(words),
        "below_96": sum(confidence < 0.96 for _, confidence in words),
        "suspicious": 0,
        "yellow": 0,
        "red": 0,
        "rows": [
            {
                "lines": [
                    {
                        "tokens": [
                            {"text": text, "confidence": confidence, "level": "ok"}
                            for text, confidence in words
                        ]
                    }
                ],
                "speaker_like": False,
            }
        ],
    }


class HybridTests(unittest.TestCase):
    def test_arabic_alignment_normalizes_harmless_variants(self) -> None:
        self.assertEqual(normalize_word("إلى"), normalize_word("الی"))

    def test_smart_mode_routes_a_low_confidence_page(self) -> None:
        report = {
            "pages": [
                pp_page(1, [("هذا", 0.99), ("نص", 0.99), ("عربي", 0.99), ("واضح", 0.99), ("وطويل", 0.99), ("للاختبار", 0.99)]),
                pp_page(2, [("الوسف", 0.947)]),
            ]
        }
        self.assertEqual(select_smart_pages(report), [2])

    def test_surya_is_primary_and_disagreement_is_flagged(self) -> None:
        pp_report = {
            "thresholds": {"yellow": 0.8, "red": 0.6},
            "pages": [pp_page(1, [("ذراعيه", 0.978)])],
        }
        surya = {
            1: {
                "page": 1,
                "blocks": [
                    {
                        "reading_order": 0,
                        "label": "Text",
                        "confidence": 0.94,
                        "html": "<p>ذراعيها</p>",
                    },
                    {
                        "reading_order": 1,
                        "label": "PageFooter",
                        "confidence": 0.94,
                        "html": "1",
                    },
                ],
            }
        }
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            report = create_hybrid_report(
                pp_report, surya, directory / "review.json", mode="best"
            )
            write_hybrid_text(report, directory / "result.txt")
            text = (directory / "result.txt").read_text(encoding="utf-8")
        self.assertIn("ذراعيها", text)
        self.assertNotIn("ذراعيه\n", text)
        self.assertEqual(report["summary"]["compared_pages"], 1)
        self.assertGreater(report["summary"]["yellow"], 0)

    def test_loads_surya_cli_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "results.json"
            path.write_text(json.dumps({"book": [{"page": 3, "blocks": []}]}), encoding="utf-8")
            result = load_surya_results(path)
        self.assertEqual(list(result), [3])

    def test_remaps_subset_relative_pages_to_original_pdf_numbers(self) -> None:
        relative = {
            1: {"page": 1, "blocks": [{"html": "page ten"}]},
            2: {"page": 2, "blocks": [{"html": "page twelve"}]},
        }
        remapped = remap_surya_pages(relative, [10, 12])
        self.assertEqual(list(remapped), [10, 12])
        self.assertEqual(remapped[12]["page"], 12)


if __name__ == "__main__":
    unittest.main()
