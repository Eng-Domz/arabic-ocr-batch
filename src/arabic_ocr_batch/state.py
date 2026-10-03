from __future__ import annotations

import csv
import json
import os
import socket
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class JobRecord:
    relative_path: str
    source_path: str
    source_fingerprint: str
    config_fingerprint: str
    status: str
    attempts: int
    output_pdf: str
    output_text: str
    owns_output_pdf: int
    owns_output_text: int
    last_error: str | None
    updated_at: float


class StateDB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=DELETE")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                relative_path TEXT PRIMARY KEY,
                source_path TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL,
                config_fingerprint TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','running','success','failed')),
                attempts INTEGER NOT NULL DEFAULT 0,
                output_pdf TEXT NOT NULL,
                output_text TEXT NOT NULL,
                owns_output_pdf INTEGER NOT NULL DEFAULT 0,
                owns_output_text INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                updated_at REAL NOT NULL
            )
            """
        )
        columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(jobs)")
        }
        added_ownership_columns = False
        for column in ("owns_output_pdf", "owns_output_text"):
            if column not in columns:
                self.connection.execute(
                    f"ALTER TABLE jobs ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                )
                added_ownership_columns = True
        if added_ownership_columns:
            self.connection.execute(
                """UPDATE jobs SET owns_output_pdf=1, owns_output_text=1
                WHERE status='success'"""
            )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "StateDB":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def get(self, relative_path: str) -> JobRecord | None:
        row = self.connection.execute(
            "SELECT * FROM jobs WHERE relative_path = ?", (relative_path,)
        ).fetchone()
        return JobRecord(**{key: row[key] for key in JobRecord.__dataclass_fields__}) if row else None

    def register(
        self,
        relative_path: str,
        source_path: Path,
        source_fingerprint: str,
        config_fingerprint: str,
        output_pdf: Path,
        output_text: Path,
    ) -> JobRecord:
        existing = self.get(relative_path)
        now = time.time()
        if existing is None:
            self.connection.execute(
                """
                INSERT INTO jobs (
                    relative_path, source_path, source_fingerprint, config_fingerprint,
                    status, attempts, output_pdf, output_text, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)
                """,
                (
                    relative_path,
                    str(source_path),
                    source_fingerprint,
                    config_fingerprint,
                    str(output_pdf),
                    str(output_text),
                    now,
                    now,
                ),
            )
        elif (
            existing.source_fingerprint != source_fingerprint
            or existing.config_fingerprint != config_fingerprint
            or existing.source_path != str(source_path)
            or existing.output_pdf != str(output_pdf)
            or existing.output_text != str(output_text)
        ):
            owns_pdf = int(
                existing.owns_output_pdf and existing.output_pdf == str(output_pdf)
            )
            owns_text = int(
                existing.owns_output_text and existing.output_text == str(output_text)
            )
            self.connection.execute(
                """
                UPDATE jobs SET source_path=?, source_fingerprint=?, config_fingerprint=?,
                    status='pending', attempts=0, output_pdf=?, output_text=?, last_error=NULL,
                    owns_output_pdf=?, owns_output_text=?, started_at=NULL,
                    finished_at=NULL, updated_at=?
                WHERE relative_path=?
                """,
                (
                    str(source_path),
                    source_fingerprint,
                    config_fingerprint,
                    str(output_pdf),
                    str(output_text),
                    owns_pdf,
                    owns_text,
                    now,
                    relative_path,
                ),
            )
        self.connection.commit()
        record = self.get(relative_path)
        assert record is not None
        return record

    def recover_interrupted_running(self) -> int:
        now = time.time()
        cursor = self.connection.execute(
            """
            UPDATE jobs SET status='failed', finished_at=?, updated_at=?,
                last_error='Interrupted previous run recovered after lock acquisition'
            WHERE status='running'
            """,
            (now, now),
        )
        self.connection.commit()
        return cursor.rowcount

    def reset_failed(self) -> int:
        now = time.time()
        cursor = self.connection.execute(
            """
            UPDATE jobs SET status='pending', attempts=0, last_error=NULL,
                started_at=NULL, finished_at=NULL, updated_at=?
            WHERE status='failed'
            """,
            (now,),
        )
        self.connection.commit()
        return cursor.rowcount

    def mark_pending(self, relative_path: str, message: str | None = None) -> None:
        now = time.time()
        self.connection.execute(
            """UPDATE jobs SET status='pending', last_error=?, updated_at=?
            WHERE relative_path=?""",
            (message, now, relative_path),
        )
        self.connection.commit()

    def mark_running(self, relative_path: str) -> None:
        now = time.time()
        self.connection.execute(
            """
            UPDATE jobs SET status='running', attempts=attempts+1, last_error=NULL,
                started_at=?, finished_at=NULL, updated_at=? WHERE relative_path=?
            """,
            (now, now, relative_path),
        )
        self.connection.commit()

    def unclaim(self, relative_path: str, message: str) -> None:
        """Undo an attempt for a future cancelled before its worker started."""
        now = time.time()
        self.connection.execute(
            """
            UPDATE jobs SET status='pending', attempts=MAX(attempts-1, 0),
                last_error=?, started_at=NULL, finished_at=NULL, updated_at=?
            WHERE relative_path=?
            """,
            (message, now, relative_path),
        )
        self.connection.commit()

    def mark_success(self, relative_path: str) -> None:
        now = time.time()
        self.connection.execute(
            """
            UPDATE jobs SET status='success', owns_output_pdf=1, owns_output_text=1,
                last_error=NULL, finished_at=?, updated_at=?
            WHERE relative_path=?
            """,
            (now, now, relative_path),
        )
        self.connection.commit()

    def mark_failed(self, relative_path: str, error: str) -> None:
        now = time.time()
        self.connection.execute(
            """
            UPDATE jobs SET status='failed', last_error=?, finished_at=?, updated_at=?
            WHERE relative_path=?
            """,
            (error[:10000], now, now, relative_path),
        )
        self.connection.commit()

    def counts(self) -> dict[str, int]:
        result = {status: 0 for status in ("pending", "running", "success", "failed")}
        for row in self.connection.execute(
            "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
        ):
            result[row["status"]] = row["count"]
        return result

    def failed_records(self) -> list[JobRecord]:
        rows = self.connection.execute(
            "SELECT * FROM jobs WHERE status='failed' ORDER BY relative_path COLLATE NOCASE"
        ).fetchall()
        return [
            JobRecord(**{key: row[key] for key in JobRecord.__dataclass_fields__})
            for row in rows
        ]

    def export_failures(self, target: Path) -> int:
        records = self.failed_records()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["relative_path", "source_path", "attempts", "last_error", "updated_at"]
            )
            for record in records:
                writer.writerow(
                    [
                        record.relative_path,
                        record.source_path,
                        record.attempts,
                        record.last_error or "",
                        time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.updated_at)
                        ),
                    ]
                )
        temporary.replace(target)
        return len(records)


class RunLockError(RuntimeError):
    """Raised when another or an unclean prior run owns the project lock."""


class RunLock:
    """Atomic cross-process lockfile suitable for a Windows-mounted project."""

    def __init__(self, path: Path, *, recover: bool = False):
        self.path = path
        self.recover = recover
        self.token = uuid.uuid4().hex
        self.acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        recovery_guard: Path | None = None
        if self.recover:
            recovery_guard = self.path.with_name(self.path.name + ".recovering")
            try:
                guard_descriptor = os.open(
                    recovery_guard,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError as exc:
                raise RunLockError(
                    f"Another process is already recovering {self.path}"
                ) from exc
            os.close(guard_descriptor)
        try:
            if self.recover:
                self._recover_existing()
            self._create_lock()
        finally:
            if recovery_guard is not None:
                recovery_guard.unlink(missing_ok=True)

    def _create_lock(self) -> None:
        metadata = {
            "token": self.token,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        try:
            descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError as exc:
            try:
                owner = self.path.read_text(encoding="utf-8").strip()
            except OSError:
                owner = "unreadable lock metadata"
            raise RunLockError(
                f"Another run or an unclean shutdown owns {self.path}: {owner}. "
                "If no OCR process is active, retry with --recover-lock."
            ) from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(metadata, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            self.path.unlink(missing_ok=True)
            raise
        self.acquired = True

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def _recover_existing(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RunLockError(f"Cannot inspect existing run lock {self.path}: {exc}") from exc
        try:
            metadata = json.loads(raw)
        except json.JSONDecodeError:
            metadata = {}
        owner_pid = metadata.get("pid")
        same_host = metadata.get("host") == socket.gethostname()
        if same_host and isinstance(owner_pid, int) and self._pid_is_alive(owner_pid):
            raise RunLockError(
                f"Refusing to recover live run lock {self.path} owned by PID {owner_pid}"
            )
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            metadata = json.loads(self.path.read_text(encoding="utf-8"))
            if metadata.get("token") == self.token:
                self.path.unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError):
            pass
        self.acquired = False

    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()

