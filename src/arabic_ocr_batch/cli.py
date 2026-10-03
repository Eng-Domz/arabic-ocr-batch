from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .config import AppConfig, ConfigError, load_config
from .discovery import DiscoveryError
from .pipeline import run_batch
from .state import RunLockError, StateDB


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arabic-ocr-batch",
        description="Restart-safe local batch OCR for Arabic PDFs",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", default="config.toml", help="TOML configuration path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="Check tools, language models, disk, and paths")
    run = subparsers.add_parser("run", help="Process pending and retryable PDFs")
    run.add_argument("--limit", type=int, help="Process at most the first N discovered PDFs")
    run.add_argument(
        "--retry-failed", action="store_true", help="Reset failed attempt counters before running"
    )
    run.add_argument(
        "--recover-lock",
        action="store_true",
        help="Replace a lock left by a confirmed-dead prior run",
    )
    subparsers.add_parser("status", help="Show persistent job counts")
    subparsers.add_parser("failures", help="Export and list current failures")
    return parser


def _setup_logger(config: AppConfig) -> tuple[logging.Logger, Path]:
    config.paths.logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = config.paths.logs_dir / f"ocr-{stamp}.log"
    logger = logging.getLogger("arabic_ocr_batch")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger, log_path


def _run_probe(command: tuple[str, ...], arguments: list[str]) -> tuple[bool, str]:
    executable = command[0]
    if not (Path(executable).is_file() or shutil.which(executable)):
        return False, f"not found: {executable}"
    try:
        result = subprocess.run(
            [*command, *arguments],
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    detail = (result.stdout or result.stderr).strip().splitlines()
    summary = detail[0] if detail else f"exit {result.returncode}"
    return result.returncode == 0, summary


def doctor(config: AppConfig) -> int:
    checks: list[tuple[str, bool, str]] = []
    for name, command, args in (
        ("OCRmyPDF", config.tools.ocrmypdf, ["--version"]),
        ("Tesseract (doctor probe)", config.tools.tesseract, ["--version"]),
        ("qpdf", config.tools.qpdf, ["--version"]),
        ("pdftotext", config.tools.pdftotext, ["-v"]),
        ("Ghostscript (doctor probe)", config.tools.ghostscript, ["--version"]),
        ("pdfinfo (doctor probe)", config.tools.pdfinfo, ["-v"]),
    ):
        ok, detail = _run_probe(command, args)
        checks.append((name, ok, detail))

    ok, language_output = _run_probe(config.tools.tesseract, ["--list-langs"])
    if ok:
        available = {line.strip() for line in language_output.splitlines()}
        # _run_probe intentionally returns one-line version details, so run the full list here.
        result = subprocess.run(
            [*config.tools.tesseract, "--list-langs"],
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
        )
        available = {line.strip() for line in result.stdout.splitlines()}
        required = set(config.ocr.languages.split("+"))
        missing = sorted(required - available)
        checks.append(
            (
                "language models",
                not missing,
                "available" if not missing else f"missing: {', '.join(missing)}",
            )
        )
    else:
        checks.append(("language models", False, language_output))

    input_ok = config.paths.input_dir.is_dir()
    checks.append(("input directory", input_ok, str(config.paths.input_dir)))
    disk_root = config.paths.input_dir if input_ok else config.config_path.parent
    try:
        free_gb = shutil.disk_usage(disk_root).free / (1024**3)
        disk_ok = free_gb >= config.runtime.minimum_free_gb
        checks.append(
            (
                "free disk",
                disk_ok,
                f"{free_gb:.1f} GiB (minimum {config.runtime.minimum_free_gb} GiB)",
            )
        )
    except OSError as exc:
        checks.append(("free disk", False, str(exc)))

    print(f"Configuration: {config.config_path}")
    print(f"Input:         {config.paths.input_dir}")
    print(f"Languages:     {config.ocr.languages}")
    for name, passed, detail in checks:
        print(f"{'OK' if passed else 'MISSING/FAIL':12} {name}: {detail}")
    if all(passed for _, passed, _ in checks):
        print("Doctor passed: the configured pipeline is ready.")
        return 0
    print("Doctor found missing requirements. See README.md for Ubuntu install commands.")
    return 1


def _status(config: AppConfig) -> int:
    if not config.paths.state_db.exists():
        print("No state database yet. Run the batch after adding sample PDFs.")
        return 0
    with StateDB(config.paths.state_db) as state:
        counts = state.counts()
    total = sum(counts.values())
    print(f"total={total}")
    for status in ("pending", "running", "success", "failed"):
        print(f"{status}={counts[status]}")
    return 0


def _failures(config: AppConfig) -> int:
    if not config.paths.state_db.exists():
        print("No state database yet; there are no recorded failures.")
        return 0
    with StateDB(config.paths.state_db) as state:
        count = state.export_failures(config.paths.failures_csv)
        records = state.failed_records()
    print(f"Exported {count} failure(s) to {config.paths.failures_csv}")
    for record in records:
        print(f"{record.relative_path} (attempts={record.attempts}): {record.last_error}")
    return 1 if records else 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="backslashreplace")
    args = _parser().parse_args(argv)
    if getattr(args, "limit", None) is not None and args.limit < 1:
        print("error: --limit must be at least 1", file=sys.stderr)
        return 2
    try:
        config = load_config(args.config)
        if args.command == "doctor":
            return doctor(config)
        if args.command == "status":
            return _status(config)
        if args.command == "failures":
            return _failures(config)
        logger, log_path = _setup_logger(config)
        logger.info("Starting run with configuration %s", config.config_path)
        summary = run_batch(
            config,
            logger,
            limit=args.limit,
            retry_failed=args.retry_failed,
            recover_lock=args.recover_lock,
        )
        logger.info("Run summary: %s", summary)
        print(
            "Summary: "
            f"discovered={summary.discovered} queued={summary.queued} "
            f"succeeded={summary.succeeded} failed={summary.failed} "
            f"skipped_success={summary.skipped_success} "
            f"skipped_running={summary.skipped_running} "
            f"skipped_attempts={summary.skipped_attempts} "
            f"discovery_errors={summary.discovery_errors}"
        )
        print(f"Detailed log: {log_path}")
        print(f"Failures CSV: {config.paths.failures_csv}")
        return 1 if summary.failed else 0
    except (ConfigError, DiscoveryError, RunLockError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted. Active OCR processes were stopped; unfinished jobs remain retryable.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

