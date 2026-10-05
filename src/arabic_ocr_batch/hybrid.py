"""Surya/PP-OCR result parsing, comparison, and combined text output."""

from __future__ import annotations

import difflib
import html
import json
import re
import unicodedata
from pathlib import Path
from typing import Iterable


SKIPPED_SURYA_LABELS = {"PageFooter", "Picture", "Figure"}
ARABIC_DIACRITICS = re.compile(r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed]")


def _plain_html(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return unicodedata.normalize("NFC", html.unescape(text)).strip()


def normalize_word(value: str) -> str:
    """Normalize harmless Arabic variants for alignment, not for published text."""
    value = unicodedata.normalize("NFKC", value).casefold().replace("ـ", "")
    value = ARABIC_DIACRITICS.sub("", value)
    value = value.translate(
        str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ی": "ي", "ک": "ك"})
    )
    return "".join(character for character in value if character.isalnum())


def _words(text: str) -> list[str]:
    return re.findall(r"\S+", text)


def pp_page_text(page: dict[str, object]) -> str:
    lines: list[str] = []
    for row in page.get("rows", []):  # type: ignore[union-attr]
        parts: list[str] = []
        for line in row.get("lines", []):  # type: ignore[union-attr]
            text = " ".join(str(token.get("text", "")) for token in line.get("tokens", []))
            if text.strip():
                parts.append(text.strip())
        if parts:
            lines.append(" ".join(parts))
    return "\n".join(lines).strip()


def load_surya_results(path: Path) -> dict[int, dict[str, object]]:
    """Load Surya's CLI JSON into a page-number mapping."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not payload:
        raise ValueError(f"Surya result has no document data: {path}")
    document = next(iter(payload.values()))
    if not isinstance(document, list):
        raise ValueError(f"Unexpected Surya result format: {path}")
    pages: dict[int, dict[str, object]] = {}
    for item in document:
        if isinstance(item, dict) and isinstance(item.get("page"), int):
            pages[int(item["page"])] = item
    return pages


def merge_surya_results(paths: Iterable[Path]) -> dict[int, dict[str, object]]:
    pages: dict[int, dict[str, object]] = {}
    for path in paths:
        pages.update(load_surya_results(path))
    return pages


def remap_surya_pages(
    pages: dict[int, dict[str, object]], actual_page_numbers: list[int]
) -> dict[int, dict[str, object]]:
    """Map Surya's subset-relative 1..N page numbers back to PDF page numbers."""
    ordered = [pages[number] for number in sorted(pages)]
    if len(ordered) != len(actual_page_numbers):
        raise ValueError(
            f"Surya returned {len(ordered)} pages for a {len(actual_page_numbers)}-page batch"
        )
    remapped: dict[int, dict[str, object]] = {}
    for actual, page in zip(actual_page_numbers, ordered, strict=True):
        copied = dict(page)
        copied["page"] = actual
        remapped[actual] = copied
    return remapped


def surya_blocks(page: dict[str, object]) -> list[dict[str, object]]:
    blocks = [
        block
        for block in page.get("blocks", [])  # type: ignore[union-attr]
        if isinstance(block, dict)
        and str(block.get("label", "")) not in SKIPPED_SURYA_LABELS
        and _plain_html(block.get("html"))
    ]
    return sorted(blocks, key=lambda block: int(block.get("reading_order", 0)))


def surya_page_text(page: dict[str, object]) -> str:
    return "\n".join(_plain_html(block.get("html")) for block in surya_blocks(page)).strip()


def select_smart_pages(report: dict[str, object]) -> list[int]:
    """Conservatively route risky PP-OCR pages to Surya.

    Confidence is intentionally not treated as correctness.  The 0.96 gate is
    combined with script anomalies and page-level confidence so confidently
    wrong tokens still have additional ways to trigger the accurate pass.
    """
    selected: list[int] = []
    for page in report.get("pages", []):  # type: ignore[union-attr]
        mean = page.get("mean_confidence")
        risky = (
            int(page.get("below_96", 0)) > 0
            or int(page.get("suspicious", 0)) > 0
            or mean is None
            or float(mean) < 0.985
            or len(pp_page_text(page)) < 30
        )
        if risky:
            selected.append(int(page["number"]))
    return selected


def _comparison_levels(surya_words: list[str], pp_words: list[str]) -> tuple[list[str], int, int]:
    surya_norm = [normalize_word(word) for word in surya_words]
    pp_norm = [normalize_word(word) for word in pp_words]
    levels = ["ok"] * len(surya_words)
    yellow = red = 0
    matcher = difflib.SequenceMatcher(a=surya_norm, b=pp_norm, autojunk=False)
    for tag, start_a, end_a, start_b, end_b in matcher.get_opcodes():
        if tag == "equal":
            continue
        level = "yellow" if tag == "replace" and (end_a - start_a) <= 3 else "red"
        for index in range(start_a, end_a):
            levels[index] = level
            if level == "yellow":
                yellow += 1
            else:
                red += 1
        if tag == "insert":
            red += max(1, end_b - start_b)
    return levels, yellow, red


def create_hybrid_report(
    pp_report: dict[str, object],
    surya_pages: dict[int, dict[str, object]],
    destination: Path,
    *,
    mode: str,
) -> dict[str, object]:
    """Build one review report with Surya as primary where it was run."""
    pp_by_number = {
        int(page["number"]): page
        for page in pp_report.get("pages", [])  # type: ignore[union-attr]
    }
    output_pages: list[dict[str, object]] = []
    total_yellow = total_red = compared_pages = 0
    for number in sorted(pp_by_number):
        pp_page = pp_by_number[number]
        pp_text = pp_page_text(pp_page)
        surya_page = surya_pages.get(number)
        if surya_page is None:
            copied = dict(pp_page)
            copied.update(engine="PP-OCR", pp_text=pp_text, surya_text=None, compared=False)
            output_pages.append(copied)
            total_yellow += int(copied.get("yellow", 0))
            total_red += int(copied.get("red", 0))
            continue

        compared_pages += 1
        primary_text = surya_page_text(surya_page)
        primary_words = _words(primary_text)
        levels, yellow, red = _comparison_levels(primary_words, _words(pp_text))
        word_index = 0
        rows: list[dict[str, object]] = []
        confidence_values: list[float] = []
        for block in surya_blocks(surya_page):
            text = _plain_html(block.get("html"))
            words = _words(text)
            try:
                confidence = float(block.get("confidence", 0))
            except (TypeError, ValueError):
                confidence = 0.0
            confidence_values.append(confidence)
            tokens = []
            for word in words:
                level = levels[word_index] if word_index < len(levels) else "red"
                tokens.append(
                    {
                        "text": word,
                        "confidence": round(confidence, 3),
                        "level": level,
                        "source": "Surya",
                    }
                )
                word_index += 1
            rows.append(
                {
                    "lines": [{"tokens": tokens}],
                    "speaker_like": str(block.get("label", "")) in {"SectionHeader", "PageHeader"},
                    "label": str(block.get("label", "Text")),
                }
            )
        total_yellow += yellow
        total_red += red
        output_pages.append(
            {
                "number": number,
                "engine": "Surya + PP-OCR check",
                "compared": True,
                "mean_confidence": round(sum(confidence_values) / len(confidence_values), 3)
                if confidence_values
                else None,
                "yellow": yellow,
                "red": red,
                "rows": rows,
                "pp_text": pp_text,
                "surya_text": primary_text,
            }
        )

    report = {
        "mode": mode,
        "thresholds": pp_report.get("thresholds", {}),
        "summary": {
            "pages": len(output_pages),
            "compared_pages": compared_pages,
            "yellow": total_yellow,
            "red": total_red,
        },
        "pages": output_pages,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(destination)
    return report


def write_hybrid_text(report: dict[str, object], destination: Path) -> None:
    pages: list[str] = []
    for page in report.get("pages", []):  # type: ignore[union-attr]
        number = int(page["number"])
        text = str(page.get("surya_text") or page.get("pp_text") or pp_page_text(page)).strip()
        pages.append(f"===== PAGE {number} =====\n{text}".rstrip())
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text("\n\n\f\n\n".join(pages) + "\n", encoding="utf-8")
    temporary.replace(destination)
