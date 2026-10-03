#!/usr/bin/env python3
"""Run Kraken once over all missing page images and emit ALTO XML."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kraken", required=True, type=Path)
    parser.add_argument("--pages", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--layout-model", required=True, type=Path)
    parser.add_argument("--recognition-model", required=True, type=Path)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--line-batch-size", type=int, default=1)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    pairs: list[tuple[Path, Path]] = []
    for page in sorted(args.pages.glob("*.png")):
        target = args.output / f"{page.stem}.alto.xml"
        if args.force or not target.exists() or target.stat().st_size == 0:
            pairs.append((page, target))
    if not pairs:
        print("All page ALTO files already exist; nothing to do.")
        return 0

    worker_count = max(1, min(args.workers, len(pairs)))
    chunks = [pairs[index::worker_count] for index in range(worker_count)]

    def build_command(chunk: list[tuple[Path, Path]]) -> list[str]:
        command = [
            str(args.kraken),
            "-a",
            "-d",
            "cpu",
            "--threads",
            str(args.threads),
        ]
        for source, target in chunk:
            command.extend(("-i", str(source), str(target)))
        command.extend(
            (
                "segment",
                "-bl",
                "-i",
                str(args.layout_model),
                "ocr",
                "-m",
                str(args.recognition_model),
                "--batch-size",
                str(args.line_batch_size),
                "--base-dir",
                "R",
            )
        )
        return command

    print(
        f"Running Kraken on {len(pairs)} page(s) with {worker_count} worker(s)...",
        flush=True,
    )
    processes = [subprocess.Popen(build_command(chunk)) for chunk in chunks]
    try:
        return_codes = [process.wait() for process in processes]
    except KeyboardInterrupt:
        for process in processes:
            process.terminate()
        for process in processes:
            process.wait()
        return 130
    failures = [code for code in return_codes if code]
    if failures:
        print(f"Kraken worker failures: {failures}")
        return failures[0]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
