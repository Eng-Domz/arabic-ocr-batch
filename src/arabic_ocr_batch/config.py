from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when configuration is missing or unsafe."""


TEXT_PIPELINE_REVISION = "pdftotext-quality-redo-v2"


@dataclass(frozen=True)
class PathsConfig:
    input_dir: Path
    searchable_dir: Path
    text_dir: Path
    work_dir: Path
    logs_dir: Path
    state_db: Path
    failures_csv: Path


@dataclass(frozen=True)
class OcrConfig:
    languages: str = "ara+eng"
    output_type: str = "pdfa"
    rotate_pages: bool = True
    deskew: bool = True
    skip_text: bool = True
    optimize: int = 1
    ocr_jobs: int = 2
    document_workers: int = 1
    max_attempts: int = 2
    timeout_seconds: int = 0
    extra_args: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RuntimeConfig:
    minimum_free_gb: int = 5


@dataclass(frozen=True)
class TextQualityConfig:
    automatic_redo: bool = True
    minimum_alphanumeric_chars_per_page: int = 10


@dataclass(frozen=True)
class ToolsConfig:
    ocrmypdf: tuple[str, ...] = ("ocrmypdf",)
    tesseract: tuple[str, ...] = ("tesseract",)
    qpdf: tuple[str, ...] = ("qpdf",)
    pdftotext: tuple[str, ...] = ("pdftotext",)
    ghostscript: tuple[str, ...] = ("gs",)
    pdfinfo: tuple[str, ...] = ("pdfinfo",)


@dataclass(frozen=True)
class AppConfig:
    config_path: Path
    paths: PathsConfig
    ocr: OcrConfig
    text_quality: TextQualityConfig
    runtime: RuntimeConfig
    tools: ToolsConfig

    def processing_fingerprint(self) -> str:
        # Only settings that can change the produced PDF or complete TXT belong here.
        # Scheduling, retry, and timeout settings must not invalidate good output.
        data = {
            "ocr": {
                "languages": self.ocr.languages,
                "output_type": self.ocr.output_type,
                "rotate_pages": self.ocr.rotate_pages,
                "deskew": self.ocr.deskew,
                "skip_text": self.ocr.skip_text,
                "optimize": self.ocr.optimize,
                "extra_args": self.ocr.extra_args,
            },
            "ocrmypdf_command": self.tools.ocrmypdf,
            "text_pipeline": {
                "revision": TEXT_PIPELINE_REVISION,
                "pdftotext_command": self.tools.pdftotext,
                "automatic_redo": self.text_quality.automatic_redo,
                "minimum_alphanumeric_chars_per_page": (
                    self.text_quality.minimum_alphanumeric_chars_per_page
                ),
            },
        }
        encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


def _absolute(base: Path, value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty path string")
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def _command(value: Any, name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        parts = tuple(shlex.split(value, posix=os.name != "nt"))
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        parts = tuple(value)
    else:
        raise ConfigError(f"tools.{name} must be a string or an array of strings")
    if not parts or not parts[0]:
        raise ConfigError(f"tools.{name} cannot be empty")
    return parts


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_paths(paths: PathsConfig) -> None:
    roots = {
        "input_dir": paths.input_dir,
        "searchable_dir": paths.searchable_dir,
        "text_dir": paths.text_dir,
        "work_dir": paths.work_dir,
    }
    names = list(roots)
    for index, left_name in enumerate(names):
        for right_name in names[index + 1 :]:
            left, right = roots[left_name], roots[right_name]
            if _is_within(left, right) or _is_within(right, left):
                raise ConfigError(
                    f"Unsafe overlapping paths: {left_name}={left} and {right_name}={right}"
                )
    for name, path in {
        "logs_dir": paths.logs_dir,
        "state_db": paths.state_db,
        "failures_csv": paths.failures_csv,
    }.items():
        if _is_within(path, paths.input_dir):
            raise ConfigError(f"Unsafe path inside input_dir: {name}={path}")


def _int(value: Any, name: str, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        upper = f" and <= {maximum}" if maximum is not None else ""
        raise ConfigError(f"{name} must be >= {minimum}{upper}")
    return value


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(
            f"Configuration file not found: {config_path}. Copy config.example.toml to config.toml."
        )
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {config_path}: {exc}") from exc

    base = config_path.parent
    path_raw = raw.get("paths", {})
    required_paths = (
        "input_dir",
        "searchable_dir",
        "text_dir",
        "work_dir",
        "logs_dir",
        "state_db",
        "failures_csv",
    )
    missing = [key for key in required_paths if key not in path_raw]
    if missing:
        raise ConfigError(f"Missing paths settings: {', '.join(missing)}")
    paths = PathsConfig(
        **{key: _absolute(base, path_raw[key], f"paths.{key}") for key in required_paths}
    )
    validate_paths(paths)

    ocr_raw = raw.get("ocr", {})
    languages = ocr_raw.get("languages", "ara+eng")
    if not isinstance(languages, str) or not re.fullmatch(
        r"[A-Za-z0-9_]+(?:\+[A-Za-z0-9_]+)*", languages
    ):
        raise ConfigError("ocr.languages must look like 'ara' or 'ara+eng'")
    output_type = ocr_raw.get("output_type", "pdfa")
    if output_type not in {"pdf", "pdfa", "pdfa-1", "pdfa-2", "pdfa-3"}:
        raise ConfigError("ocr.output_type must be pdf, pdfa, pdfa-1, pdfa-2, or pdfa-3")
    extra_args = ocr_raw.get("extra_args", [])
    if not isinstance(extra_args, list) or not all(isinstance(arg, str) for arg in extra_args):
        raise ConfigError("ocr.extra_args must be an array of strings")
    reserved = {
        "--sidecar",
        "--jobs",
        "-j",
        "--language",
        "-l",
        "--output-type",
        "--optimize",
        "--rotate-pages",
        "--deskew",
        "--skip-text",
        "--redo-ocr",
        "--force-ocr",
    }
    for arg in extra_args:
        if arg.split("=", 1)[0] in reserved:
            raise ConfigError(f"ocr.extra_args cannot override managed option {arg!r}")
    bool_names = ("rotate_pages", "deskew", "skip_text")
    for name in bool_names:
        if name in ocr_raw and not isinstance(ocr_raw[name], bool):
            raise ConfigError(f"ocr.{name} must be true or false")
    ocr = OcrConfig(
        languages=languages,
        output_type=output_type,
        rotate_pages=ocr_raw.get("rotate_pages", True),
        deskew=ocr_raw.get("deskew", True),
        skip_text=ocr_raw.get("skip_text", True),
        optimize=_int(ocr_raw.get("optimize", 1), "ocr.optimize", 0, 3),
        ocr_jobs=_int(ocr_raw.get("ocr_jobs", 2), "ocr.ocr_jobs", 1),
        document_workers=_int(
            ocr_raw.get("document_workers", 1), "ocr.document_workers", 1
        ),
        max_attempts=_int(ocr_raw.get("max_attempts", 2), "ocr.max_attempts", 1),
        timeout_seconds=_int(
            ocr_raw.get("timeout_seconds", 0), "ocr.timeout_seconds", 0
        ),
        extra_args=tuple(extra_args),
    )

    text_quality_raw = raw.get("text_quality", {})
    automatic_redo = text_quality_raw.get("automatic_redo", True)
    if not isinstance(automatic_redo, bool):
        raise ConfigError("text_quality.automatic_redo must be true or false")
    text_quality = TextQualityConfig(
        automatic_redo=automatic_redo,
        minimum_alphanumeric_chars_per_page=_int(
            text_quality_raw.get("minimum_alphanumeric_chars_per_page", 10),
            "text_quality.minimum_alphanumeric_chars_per_page",
            1,
        ),
    )

    runtime_raw = raw.get("runtime", {})
    runtime = RuntimeConfig(
        minimum_free_gb=_int(
            runtime_raw.get("minimum_free_gb", 5), "runtime.minimum_free_gb", 0
        ),
    )
    tools_raw = raw.get("tools", {})
    tools = ToolsConfig(
        **{
            name: _command(tools_raw.get(name, default[0]), name)
            for name, default in asdict(ToolsConfig()).items()
        }
    )
    return AppConfig(config_path, paths, ocr, text_quality, runtime, tools)

