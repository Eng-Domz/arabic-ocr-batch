from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class DiscoveryError(RuntimeError):
    """Raised when source discovery would create ambiguous outputs."""


@dataclass(frozen=True)
class PdfSource:
    path: Path
    relative: Path
    size: int
    mtime_ns: int
    error: str | None = None


def discover_pdfs(
    input_dir: Path, *, on_walk_error: Callable[[OSError], None] | None = None
) -> list[PdfSource]:
    if not input_dir.is_dir():
        raise DiscoveryError(f"Input directory does not exist: {input_dir}")
    found: list[PdfSource] = []
    def handle_walk_error(error: OSError) -> None:
        if on_walk_error is not None:
            on_walk_error(error)
            return
        raise DiscoveryError(f"Could not scan {error.filename or input_dir}: {error}")

    for root, dirnames, filenames in os.walk(
        input_dir, followlinks=False, onerror=handle_walk_error
    ):
        root_path = Path(root)
        safe_directories: list[str] = []
        for name in dirnames:
            directory = root_path / name
            try:
                if not directory.is_symlink():
                    safe_directories.append(name)
            except OSError as exc:
                handle_walk_error(exc)
        dirnames[:] = sorted(safe_directories, key=str.casefold)
        for filename in sorted(filenames, key=str.casefold):
            path = root_path / filename
            if path.suffix.casefold() != ".pdf":
                continue
            relative = path.relative_to(input_dir)
            try:
                if path.is_symlink():
                    continue
                stat = path.stat()
                found.append(PdfSource(path, relative, stat.st_size, stat.st_mtime_ns))
            except OSError as exc:
                found.append(
                    PdfSource(
                        path,
                        relative,
                        0,
                        0,
                        f"Could not inspect source: {type(exc).__name__}: {exc}",
                    )
                )
    found.sort(key=lambda item: (item.relative.as_posix().casefold(), item.relative.as_posix()))
    collisions: dict[str, Path] = {}
    for source in found:
        key = source.relative.with_suffix(".pdf").as_posix().casefold()
        previous = collisions.get(key)
        if previous is not None:
            raise DiscoveryError(
                f"Output collision between {previous.as_posix()!r} and "
                f"{source.relative.as_posix()!r}; rename one source file"
            )
        collisions[key] = source.relative
    return found


def source_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

