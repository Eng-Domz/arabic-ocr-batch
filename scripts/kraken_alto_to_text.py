#!/usr/bin/env python3
"""Convert Kraken ALTO XML pages into reading-order UTF-8 text.

Kraken can emit separate text blocks for marginal speaker names and the main
dialogue column. This converter ignores block serialization order and rebuilds
rows from the line coordinates: top-to-bottom between rows and right-to-left
within a row.
"""

from __future__ import annotations

import argparse
import re
import statistics
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Line:
    text: str
    x: int
    y: int
    width: int
    height: int

    @property
    def center_y(self) -> float:
        return self.y + self.height / 2


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _line_text(element: ET.Element) -> str:
    words = [
        child.attrib.get("CONTENT", "").strip()
        for child in element.iter()
        if _local_name(child.tag) == "String"
        and child.attrib.get("CONTENT", "").strip()
    ]
    text = " ".join(words)
    return unicodedata.normalize("NFC", text).strip()


def read_alto(path: Path) -> tuple[int, list[Line]]:
    root = ET.parse(path).getroot()
    page = next((item for item in root.iter() if _local_name(item.tag) == "Page"), None)
    page_width = int(page.attrib.get("WIDTH", "0")) if page is not None else 0
    lines: list[Line] = []
    for element in root.iter():
        if _local_name(element.tag) != "TextLine":
            continue
        text = _line_text(element)
        if not text or text.casefold() == "ss":
            continue
        lines.append(
            Line(
                text=text,
                x=int(element.attrib.get("HPOS", "0")),
                y=int(element.attrib.get("VPOS", "0")),
                width=int(element.attrib.get("WIDTH", "0")),
                height=max(1, int(element.attrib.get("HEIGHT", "1"))),
            )
        )
    return page_width, lines


def _cluster_rows(lines: list[Line]) -> list[list[Line]]:
    if not lines:
        return []
    median_height = statistics.median(line.height for line in lines)
    tolerance = max(12.0, median_height * 0.55)
    rows: list[list[Line]] = []
    row_center = 0.0
    for line in sorted(lines, key=lambda item: (item.center_y, -item.x)):
        if not rows or abs(line.center_y - row_center) > tolerance:
            rows.append([line])
            row_center = line.center_y
        else:
            rows[-1].append(line)
            row_center = sum(item.center_y for item in rows[-1]) / len(rows[-1])
    return rows


def page_text(path: Path) -> str:
    page_width, lines = read_alto(path)
    output: list[str] = []
    for row in _cluster_rows(lines):
        ordered = sorted(row, key=lambda item: item.x, reverse=True)
        parts = [item.text for item in ordered]
        if len(ordered) >= 2 and page_width:
            first = ordered[0]
            speaker_like = first.x >= page_width * 0.70 and first.width <= page_width * 0.18
            if speaker_like:
                speaker = first.text.rstrip(":؛ ")
                parts[0] = f"{speaker}:"
                if len(parts) > 1:
                    parts[1] = parts[1].lstrip(":\u061b ")
        output.append(" ".join(parts))
    return "\n".join(output).strip()


def convert(inputs: list[Path], output: Path) -> None:
    pages = []
    for index, path in enumerate(sorted(inputs), start=1):
        text = page_text(path)
        match = re.search(r"(\d+)(?!.*\d)", path.stem)
        page_number = int(match.group(1)) if match else index
        pages.append(f"===== PAGE {page_number} =====\n{text}".rstrip())
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text("\n\n\f\n\n".join(pages) + "\n", encoding="utf-8")
    temporary.replace(output)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    convert(args.inputs, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
