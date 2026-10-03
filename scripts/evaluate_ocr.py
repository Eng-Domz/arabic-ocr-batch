#!/usr/bin/env python3
"""Measure Arabic character error rate against manually verified pages."""

from __future__ import annotations

import argparse
import re
import unicodedata
from pathlib import Path


def normalize_arabic(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(
        str.maketrans({"ی": "ي", "ک": "ك", "گ": "ك", "پ": "ب", "ى": "ي"})
    )
    return "".join(re.findall(r"[\u0621-\u064a]", text))


def edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, start=1):
        current = [row]
        for column, right_char in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def extract_page(text: str, page_number: int) -> str:
    match = re.search(
        rf"(?s)===== PAGE {page_number} =====\r?\n(.*?)(?=\r?\n\r?\n\f|\Z)",
        text,
    )
    if not match:
        raise ValueError(f"page marker {page_number} was not found")
    return match.group(1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument(
        "--reference",
        action="append",
        required=True,
        metavar="PAGE=FILE",
        help="manually verified page number and UTF-8 text file",
    )
    args = parser.parse_args()

    candidate = args.candidate.read_text(encoding="utf-8")
    total_errors = 0
    total_characters = 0
    for item in args.reference:
        page_text, separator, file_text = item.partition("=")
        if not separator or not page_text.isdigit():
            parser.error(f"invalid reference {item!r}; expected PAGE=FILE")
        page_number = int(page_text)
        reference = normalize_arabic(Path(file_text).read_text(encoding="utf-8"))
        recognized = normalize_arabic(extract_page(candidate, page_number))
        errors = edit_distance(reference, recognized)
        characters = len(reference)
        total_errors += errors
        total_characters += characters
        print(
            f"page={page_number} errors={errors} characters={characters} "
            f"cer={100 * errors / characters:.2f}%"
        )

    print(
        f"total errors={total_errors} characters={total_characters} "
        f"cer={100 * total_errors / total_characters:.2f}%"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
