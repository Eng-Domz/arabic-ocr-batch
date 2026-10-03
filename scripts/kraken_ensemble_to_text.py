#!/usr/bin/env python3
"""Merge two Kraken ALTO runs into cleaner right-to-left text.

The PP-OCRv6 recognizer is stronger on normal prose in this book, while the
OpenITI recognizer is more reliable on the very short speaker-name blocks.
Both runs use the same segmentation model, so their text-line coordinates can
be matched without guessing from the recognized text.
"""

from __future__ import annotations

import argparse
import difflib
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

from kraken_alto_to_text import Line, _cluster_rows, read_alto


SPEAKER_NAMES = (
    "يحيى",
    "راغب",
    "شوشو",
    "بسبس",
    "منير",
    "شعبان",
    "الوكيل",
    "الجراح",
    "المأمور",
    "الشرطي",
    "الجميع",
)

ARABIC_TRANSLATION = str.maketrans(
    {
        "ی": "ي",
        "ک": "ك",
        "گ": "ك",
        "پ": "ب",
        "۰": "٠",
        "۱": "١",
        "۲": "٢",
        "۳": "٣",
        "۴": "٤",
        "۵": "٥",
        "۶": "٦",
        "۷": "٧",
        "۸": "٨",
        "۹": "٩",
        "?": "؟",
    }
)

# Frequent character-level confusions observed in the speaker names of this
# book. They are safe to normalize globally because these are proper names,
# not ordinary Arabic words in this text.
NAME_ALIASES = {
    "يحيي": "يحيى",
    "يحي": "يحيى",
    "بحيي": "يحيى",
    "بحي": "يحيى",
    "حيي": "يحيى",
    "يجي": "يحيى",
    "بسيس": "بسبس",
    "يسيس": "بسبس",
    "بسيبس": "بسبس",
    "بسيسس": "بسبس",
    "مثیر": "منير",
    "مثير": "منير",
    "مشير": "منير",
    "هشير": "منير",
    "متير": "منير",
    "هثير": "منير",
    "عثير": "منير",
    "مشر": "منير",
    "منيل": "منير",
    "شعيان": "شعبان",
    "اغب": "راغب",
    "لوكيل": "الوكيل",
    "وكيل": "الوكيل",
}

INLINE_ALIASES = {
    "بسيس": "بسبس",
    "يسيس": "بسبس",
    "بسيبس": "بسبس",
    "بسيسس": "بسبس",
    "يحيي": "يحيى",
    "بحيي": "يحيى",
    "يحبى": "يحيى",
    "يشاءب": "يتثاءب",
}

TOKEN_ALIASES = {"يه": "إيه"}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _page_height(path: Path) -> int:
    root = ET.parse(path).getroot()
    page = next((item for item in root.iter() if _local_name(item.tag) == "Page"), None)
    return int(page.attrib.get("HEIGHT", "0")) if page is not None else 0


def _arabic_count(text: str) -> int:
    return len(re.findall(r"[\u0621-\u064a]", text))


def _match_line(line: Line, candidates: list[Line]) -> Line | None:
    if not candidates:
        return None
    ranked = sorted(
        candidates,
        key=lambda item: (
            abs(item.center_y - line.center_y),
            abs(item.x - line.x),
            abs(item.width - line.width),
        ),
    )
    best = ranked[0]
    vertical_limit = max(line.height, best.height) * 0.55 + 8
    return best if abs(best.center_y - line.center_y) <= vertical_limit else None


def _speaker_key(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(ARABIC_TRANSLATION)
    text = text.translate(str.maketrans({"ى": "ي", "ئ": "ي", "أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه"}))
    return "".join(re.findall(r"[\u0621-\u064a]", text))


def _canonical_speaker(text: str) -> str:
    key = _speaker_key(text)
    if not key:
        return text.strip()
    if key in NAME_ALIASES:
        return NAME_ALIASES[key]
    scored = [
        (difflib.SequenceMatcher(None, key, _speaker_key(name)).ratio(), name)
        for name in SPEAKER_NAMES
    ]
    score, name = max(scored)
    return name if score >= 0.60 else text.strip().rstrip(":؛ ")


def _is_speaker(line: Line, page_width: int) -> bool:
    return bool(page_width) and line.x >= page_width * 0.70 and line.width <= page_width * 0.18


def _is_short_header(line: Line, page_height: int) -> bool:
    compact = re.sub(r"\s+", "", line.text)
    return bool(page_height) and line.center_y < page_height * 0.18 and len(compact) <= 16


def _select_lines(primary_path: Path, fallback_path: Path | None) -> tuple[int, list[Line]]:
    page_width, primary = read_alto(primary_path)
    page_height = _page_height(primary_path)
    fallback = read_alto(fallback_path)[1] if fallback_path and fallback_path.exists() else []

    selected: list[Line] = []
    for line in primary:
        old = _match_line(line, fallback)
        text = line.text

        if _is_short_header(line, page_height):
            if old and _arabic_count(old.text) >= 2:
                text = old.text
            elif _arabic_count(text) < 2:
                continue
        elif _is_speaker(line, page_width):
            text = old.text if old and old.text.strip() else text
            text = _canonical_speaker(text)
        elif _arabic_count(text) == 0 and old and _arabic_count(old.text) > 0:
            text = old.text

        text = unicodedata.normalize("NFC", text).translate(ARABIC_TRANSLATION).strip()
        if not text or text.casefold() == "ss":
            continue
        selected.append(replace(line, text=text))
    return page_width, selected


def page_text(primary_path: Path, fallback_path: Path | None) -> str:
    page_width, lines = _select_lines(primary_path, fallback_path)
    output: list[str] = []
    for row in _cluster_rows(lines):
        ordered = sorted(row, key=lambda item: item.x, reverse=True)
        parts = [item.text for item in ordered]
        if len(ordered) >= 2 and _is_speaker(ordered[0], page_width):
            parts[0] = f"{_canonical_speaker(parts[0])}:"
            parts[1] = parts[1].lstrip(":؛ ")
        text = " ".join(parts)
        for source, target in INLINE_ALIASES.items():
            text = text.replace(source, target)
        for source, target in TOKEN_ALIASES.items():
            text = re.sub(
                rf"(?<![\u0621-\u064a]){re.escape(source)}(?![\u0621-\u064a])",
                target,
                text,
            )
        output.append(text)
    return "\n".join(output).strip()


def convert(primary_dir: Path, fallback_dir: Path, output: Path) -> None:
    primary_files = sorted(primary_dir.glob("*.xml"))
    if not primary_files:
        raise SystemExit(f"No ALTO XML files found in {primary_dir}")

    pages: list[str] = []
    for index, primary_path in enumerate(primary_files, start=1):
        fallback_path = fallback_dir / primary_path.name
        match = re.search(r"(\d+)(?!.*\d)", primary_path.stem)
        page_number = int(match.group(1)) if match else index
        text = page_text(primary_path, fallback_path)
        pages.append(f"===== PAGE {page_number} =====\n{text}".rstrip())

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text("\n\n\f\n\n".join(pages) + "\n", encoding="utf-8")
    temporary.replace(output)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--primary-dir", required=True, type=Path)
    parser.add_argument("--fallback-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    convert(args.primary_dir, args.fallback_dir, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
