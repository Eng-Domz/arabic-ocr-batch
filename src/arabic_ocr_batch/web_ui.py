from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
import webbrowser
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[2]
UI_HTML = Path(__file__).with_name("ui.html")
UPLOAD_DIR = PROJECT_ROOT / "input" / "ui_uploads"
JOB_ROOT = PROJECT_ROOT / "work" / "ui"
OUTPUT_ROOT = PROJECT_ROOT / "output" / "ui"
STATE_ROOT = PROJECT_ROOT / "state" / "ui"

DEFAULT_KRAKEN = (
    PROJECT_ROOT / "work" / "engine-lab" / "kraken" / ".venv" / "bin" / "kraken"
)
DEFAULT_LAYOUT = Path.home() / ".local/share/arabic-ocr-batch/models/AOCP_print_models/layout/layout-20210711_AQ.mlmodel"
DEFAULT_RECOGNITION = Path.home() / ".local/share/arabic-ocr-batch/models/ppocrv6/medium.safetensors"

MAX_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024
YELLOW_THRESHOLD = 0.80
RED_THRESHOLD = 0.60


class UIError(ValueError):
    pass


def safe_output_name(value: str) -> str:
    value = unicodedata.normalize("NFC", value).strip()
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value)
    value = value.rstrip(". ")
    return value[:160] or "ocr-result"


def parse_page_selection(value: str, page_count: int) -> list[int]:
    if page_count < 1:
        raise UIError("The PDF has no pages.")
    value = value.strip()
    if not value or value.casefold() == "all":
        return list(range(1, page_count + 1))
    pages: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            raise UIError("Page ranges cannot contain an empty item.")
        match = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", part)
        if not match:
            raise UIError(f"Invalid page item: {part!r}. Use a format such as 1-5,8,10-12.")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start > end:
            raise UIError(f"Page range starts after it ends: {part!r}.")
        if start < 1 or end > page_count:
            raise UIError(f"Page range {part!r} is outside 1-{page_count}.")
        pages.update(range(start, end + 1))
    return sorted(pages)


def contiguous_ranges(pages: list[int]) -> list[tuple[int, int]]:
    if not pages:
        return []
    result: list[tuple[int, int]] = []
    start = previous = pages[0]
    for page in pages[1:]:
        if page != previous + 1:
            result.append((start, previous))
            start = page
        previous = page
    result.append((start, previous))
    return result


def pdf_page_count(path: Path) -> int:
    result = subprocess.run(
        ["pdfinfo", str(path)], capture_output=True, text=True, errors="replace", timeout=30
    )
    if result.returncode:
        raise UIError((result.stderr or result.stdout or "pdfinfo failed").strip())
    match = re.search(r"^Pages:\s+(\d+)\s*$", result.stdout, re.MULTILINE)
    if not match:
        raise UIError("Could not determine the PDF page count.")
    return int(match.group(1))


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@dataclass(frozen=True)
class ReviewLine:
    x: int
    y: int
    width: int
    height: int
    tokens: list[dict[str, object]]

    @property
    def center_y(self) -> float:
        return self.y + self.height / 2


def _review_page(path: Path, page_number: int) -> dict[str, object]:
    root = ET.parse(path).getroot()
    page_element = next((e for e in root.iter() if _local_name(e.tag) == "Page"), None)
    page_width = int(page_element.attrib.get("WIDTH", "0")) if page_element is not None else 0
    lines: list[ReviewLine] = []
    all_confidences: list[float] = []
    yellow = red = 0
    for element in root.iter():
        if _local_name(element.tag) != "TextLine":
            continue
        tokens: list[dict[str, object]] = []
        for child in element.iter():
            if _local_name(child.tag) != "String":
                continue
            text = unicodedata.normalize("NFC", child.attrib.get("CONTENT", "").strip())
            if not text:
                continue
            try:
                confidence = float(child.attrib.get("WC", "1"))
            except ValueError:
                confidence = 1.0
            reviewable = any(character.isalnum() for character in text)
            level = "ok"
            if reviewable and confidence < RED_THRESHOLD:
                level = "red"
                red += 1
            elif reviewable and confidence < YELLOW_THRESHOLD:
                level = "yellow"
                yellow += 1
            all_confidences.append(confidence)
            tokens.append({"text": text, "confidence": round(confidence, 3), "level": level})
        if not tokens or " ".join(str(t["text"]) for t in tokens).casefold() == "ss":
            continue
        lines.append(
            ReviewLine(
                x=int(element.attrib.get("HPOS", "0")),
                y=int(element.attrib.get("VPOS", "0")),
                width=int(element.attrib.get("WIDTH", "0")),
                height=max(1, int(element.attrib.get("HEIGHT", "1"))),
                tokens=tokens,
            )
        )

    if lines:
        heights = sorted(line.height for line in lines)
        median_height = heights[len(heights) // 2]
        tolerance = max(12.0, median_height * 0.55)
    else:
        tolerance = 12.0
    rows: list[list[ReviewLine]] = []
    row_center = 0.0
    for line in sorted(lines, key=lambda item: (item.center_y, -item.x)):
        if not rows or abs(line.center_y - row_center) > tolerance:
            rows.append([line])
        else:
            rows[-1].append(line)
        row_center = sum(item.center_y for item in rows[-1]) / len(rows[-1])

    output_rows: list[dict[str, object]] = []
    for row in rows:
        ordered = sorted(row, key=lambda item: item.x, reverse=True)
        output_rows.append(
            {
                "lines": [{"tokens": line.tokens} for line in ordered],
                "speaker_like": bool(
                    len(ordered) >= 2
                    and page_width
                    and ordered[0].x >= page_width * 0.70
                    and ordered[0].width <= page_width * 0.18
                ),
            }
        )
    return {
        "number": page_number,
        "mean_confidence": round(sum(all_confidences) / len(all_confidences), 3)
        if all_confidences
        else None,
        "yellow": yellow,
        "red": red,
        "rows": output_rows,
    }


def create_review_report(alto_dir: Path, destination: Path) -> dict[str, object]:
    pages: list[dict[str, object]] = []
    for path in sorted(alto_dir.glob("*.alto.xml")):
        match = re.search(r"(\d+)(?!.*\d)", path.stem)
        if not match:
            continue
        pages.append(_review_page(path, int(match.group(1))))
    report = {
        "thresholds": {"yellow": YELLOW_THRESHOLD, "red": RED_THRESHOLD},
        "summary": {
            "pages": len(pages),
            "yellow": sum(int(page["yellow"]) for page in pages),
            "red": sum(int(page["red"]) for page in pages),
        },
        "pages": pages,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(destination)
    return report


class JobManager:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.jobs: dict[str, dict[str, object]] = {}
        for directory in (UPLOAD_DIR, JOB_ROOT, OUTPUT_ROOT, STATE_ROOT):
            directory.mkdir(parents=True, exist_ok=True)
        self._load_jobs()

    def _load_jobs(self) -> None:
        for state_path in STATE_ROOT.glob("*.json"):
            try:
                job = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if job.get("status") == "running":
                job["status"] = "failed"
                job["message"] = "The interface stopped during this job. Press Retry to resume it."
            self.jobs[str(job["id"])] = job
            self._save(job)

    def _public(self, job: dict[str, object]) -> dict[str, object]:
        allowed = (
            "id", "name", "status", "stage", "message", "progress", "completed_pages",
            "total_pages", "pages", "outputs", "review", "created_at",
        )
        return {key: job.get(key) for key in allowed}

    def _save(self, job: dict[str, object]) -> None:
        target = STATE_ROOT / f"{job['id']}.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)

    def list_jobs(self) -> list[dict[str, object]]:
        with self.lock:
            ordered = sorted(self.jobs.values(), key=lambda item: str(item["created_at"]), reverse=True)
            return [self._public(job) for job in ordered[:20]]

    def get(self, job_id: str) -> dict[str, object]:
        with self.lock:
            try:
                return self._public(self.jobs[job_id])
            except KeyError as exc:
                raise UIError("Unknown job.") from exc

    def _update(self, job: dict[str, object], **values: object) -> None:
        with self.lock:
            job.update(values)
            self._save(job)

    def create(self, source: Path, page_count: int, payload: dict[str, object]) -> dict[str, object]:
        pages = parse_page_selection(str(payload.get("pages", "all")), page_count)
        output_name = safe_output_name(str(payload.get("output_name") or source.stem))
        job_id = uuid.uuid4().hex[:12]
        job: dict[str, object] = {
            "id": job_id,
            "source": str(source),
            "name": output_name,
            "status": "queued",
            "stage": "Waiting",
            "message": "Queued",
            "progress": 0,
            "completed_pages": 0,
            "total_pages": len(pages),
            "pages": pages,
            "create_searchable_pdf": bool(payload.get("create_searchable_pdf", False)),
            "outputs": [],
            "review": None,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        with self.lock:
            if any(existing.get("status") == "running" for existing in self.jobs.values()):
                raise UIError("Another OCR job is running. Wait for it to finish before starting another.")
            self.jobs[job_id] = job
            self._save(job)
        threading.Thread(target=self._run, args=(job,), daemon=True, name=f"ocr-{job_id}").start()
        return self._public(job)

    def retry(self, job_id: str) -> dict[str, object]:
        with self.lock:
            if any(existing.get("status") == "running" for existing in self.jobs.values()):
                raise UIError("Another OCR job is running.")
            try:
                job = self.jobs[job_id]
            except KeyError as exc:
                raise UIError("Unknown job.") from exc
            if job.get("status") not in {"failed", "complete"}:
                raise UIError("Only completed or failed jobs can be retried.")
            job.update(status="queued", stage="Waiting", message="Queued for retry")
            self._save(job)
        threading.Thread(target=self._run, args=(job,), daemon=True, name=f"ocr-{job_id}").start()
        return self._public(job)

    def _command(self, job: dict[str, object], command: list[str], stage: str) -> None:
        self._update(job, stage=stage, message=stage)
        result = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, errors="replace")
        if result.returncode:
            detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
            raise RuntimeError(f"{stage} failed: {detail[-3000:]}")

    def _render_pages(self, job: dict[str, object], pages_dir: Path) -> None:
        source = Path(str(job["source"]))
        pages = [int(page) for page in job["pages"]]  # type: ignore[index]
        pages_dir.mkdir(parents=True, exist_ok=True)
        for index, page in enumerate(pages, start=1):
            target = pages_dir / f"page-{page:04d}.png"
            if not target.exists() or target.stat().st_size == 0:
                self._command(
                    job,
                    [
                        "pdftoppm", "-f", str(page), "-l", str(page), "-singlefile",
                        "-r", "300", "-png", str(source), str(target.with_suffix("")),
                    ],
                    f"Rendering page {page}",
                )
            self._update(
                job,
                completed_pages=index,
                progress=round(index / max(1, len(pages)) * 20),
                message=f"Rendered page {index} of {len(pages)}",
            )

    def _run(self, job: dict[str, object]) -> None:
        job_id = str(job["id"])
        work_dir = JOB_ROOT / job_id
        pages_dir = work_dir / "pages"
        alto_dir = work_dir / "alto"
        output_dir = OUTPUT_ROOT / job_id
        output_dir.mkdir(parents=True, exist_ok=True)
        log_path = output_dir / "process.log"
        try:
            self._update(job, status="running", stage="Starting", message="Starting OCR", progress=1)
            self._render_pages(job, pages_dir)
            kraken = Path(os.environ.get("ARABIC_OCR_KRAKEN", str(DEFAULT_KRAKEN))).expanduser()
            layout = Path(os.environ.get("ARABIC_OCR_LAYOUT_MODEL", str(DEFAULT_LAYOUT))).expanduser()
            recognition = Path(
                os.environ.get("ARABIC_OCR_RECOGNITION_MODEL", str(DEFAULT_RECOGNITION))
            ).expanduser()
            for required, label in ((kraken, "Kraken"), (layout, "layout model"), (recognition, "recognition model")):
                if not required.is_file():
                    raise RuntimeError(f"Missing {label}: {required}")

            self._update(job, stage="Recognizing Arabic text", message="High-accuracy OCR is running", progress=22)
            alto_dir.mkdir(parents=True, exist_ok=True)
            runner = PROJECT_ROOT / "scripts" / "run_kraken_batch.py"
            with log_path.open("a", encoding="utf-8") as log:
                process = subprocess.Popen(
                    [
                        sys.executable, str(runner), "--kraken", str(kraken), "--pages", str(pages_dir),
                        "--output", str(alto_dir), "--layout-model", str(layout),
                        "--recognition-model", str(recognition), "--threads", "2",
                        "--line-batch-size", "1", "--workers", "3",
                    ],
                    cwd=PROJECT_ROOT,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    errors="replace",
                )
                while process.poll() is None:
                    completed = sum(1 for path in alto_dir.glob("*.alto.xml") if path.stat().st_size)
                    total = int(job["total_pages"])
                    self._update(
                        job,
                        completed_pages=completed,
                        progress=22 + round(completed / max(1, total) * 63),
                        message=f"Recognized {completed} of {total} pages",
                    )
                    time.sleep(0.5)
                return_code = process.wait()
            if return_code:
                raise RuntimeError(f"Kraken OCR failed with exit code {return_code}. See {log_path}.")

            text_path = output_dir / f"{job['name']}.txt"
            converter = PROJECT_ROOT / "scripts" / "kraken_alto_to_text.py"
            alto_files = [str(path) for path in sorted(alto_dir.glob("*.alto.xml"))]
            self._command(
                job,
                [sys.executable, str(converter), *alto_files, "--output", str(text_path)],
                "Building UTF-8 text",
            )
            report_path = output_dir / "review.json"
            report = create_review_report(alto_dir, report_path)
            outputs = [
                {"label": "High-accuracy text", "name": text_path.name},
                {"label": "Confidence report", "name": report_path.name},
            ]

            if bool(job.get("create_searchable_pdf")):
                selected_pdf = work_dir / "selected-pages.pdf"
                page_spec = ",".join(str(page) for page in job["pages"])  # type: ignore[index]
                self._command(
                    job,
                    ["qpdf", str(job["source"]), "--pages", ".", page_spec, "--", str(selected_pdf)],
                    "Selecting PDF pages",
                )
                searchable = output_dir / f"{job['name']} - searchable.pdf"
                self._command(
                    job,
                    [
                        "ocrmypdf", "-l", "ara+eng", "--rotate-pages", "--deskew", "--skip-text",
                        "--output-type", "pdfa", "--optimize", "1", "--jobs", "2",
                        str(selected_pdf), str(searchable),
                    ],
                    "Creating searchable PDF",
                )
                outputs.append({"label": "Searchable PDF", "name": searchable.name})

            review = report["summary"]
            message = "Finished"
            if int(review["yellow"]) or int(review["red"]):  # type: ignore[index]
                message = f"Finished — {review['yellow']} check and {review['red']} urgent review labels"
            self._update(
                job,
                status="complete",
                stage="Complete",
                message=message,
                progress=100,
                completed_pages=job["total_pages"],
                outputs=outputs,
                review=review,
            )
        except Exception as exc:
            self._update(job, status="failed", stage="Needs attention", message=str(exc), progress=job.get("progress", 0))


MANAGER = JobManager()


class Handler(BaseHTTPRequestHandler):
    server_version = "ArabicOCRLocal/0.1"

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

    def _json(self, payload: object, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, exc: Exception, status: int = 400) -> None:
        self._json({"error": str(exc)}, status)

    def _read_json(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > 1_000_000:
            raise UIError("Invalid request size.")
        try:
            value = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise UIError("Invalid JSON request.") from exc
        if not isinstance(value, dict):
            raise UIError("The request must be an object.")
        return value

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                data = UI_HTML.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if parsed.path == "/api/jobs":
                self._json({"jobs": MANAGER.list_jobs()})
                return
            match = re.fullmatch(r"/api/jobs/([a-f0-9]{12})", parsed.path)
            if match:
                self._json(MANAGER.get(match.group(1)))
                return
            match = re.fullmatch(r"/api/jobs/([a-f0-9]{12})/review", parsed.path)
            if match:
                path = OUTPUT_ROOT / match.group(1) / "review.json"
                if not path.is_file():
                    raise UIError("The review report is not ready yet.")
                self._json(json.loads(path.read_text(encoding="utf-8")))
                return
            match = re.fullmatch(r"/api/jobs/([a-f0-9]{12})/page/(\d+)", parsed.path)
            if match:
                path = JOB_ROOT / match.group(1) / "pages" / f"page-{int(match.group(2)):04d}.png"
                self._send_file(path, "image/png", download=False)
                return
            match = re.fullmatch(r"/api/jobs/([a-f0-9]{12})/download/(.+)", parsed.path)
            if match:
                name = Path(unquote(match.group(2))).name
                path = OUTPUT_ROOT / match.group(1) / name
                content_type = "application/pdf" if path.suffix.casefold() == ".pdf" else "text/plain; charset=utf-8"
                if path.suffix.casefold() == ".json":
                    content_type = "application/json; charset=utf-8"
                self._send_file(path, content_type, download=True)
                return
            self.send_error(404)
        except (UIError, OSError, KeyError) as exc:
            self._error(exc, 404)

    def _send_file(self, path: Path, content_type: str, *, download: bool) -> None:
        if not path.is_file():
            raise UIError("File not found.")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(path.stat().st_size))
        if download:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(path.name)}")
        self.end_headers()
        with path.open("rb") as source:
            shutil.copyfileobj(source, self.wfile, length=1024 * 1024)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/upload":
                self._upload(parsed)
                return
            if parsed.path == "/api/jobs":
                payload = self._read_json()
                upload_id = str(payload.get("upload_id", ""))
                if not re.fullmatch(r"[a-f0-9]{16}", upload_id):
                    raise UIError("Invalid upload.")
                source = UPLOAD_DIR / f"{upload_id}.pdf"
                if not source.is_file():
                    raise UIError("Uploaded PDF was not found.")
                page_count = pdf_page_count(source)
                self._json(MANAGER.create(source, page_count, payload), HTTPStatus.CREATED)
                return
            match = re.fullmatch(r"/api/jobs/([a-f0-9]{12})/retry", parsed.path)
            if match:
                self._json(MANAGER.retry(match.group(1)))
                return
            self.send_error(404)
        except (UIError, OSError, subprocess.SubprocessError) as exc:
            self._error(exc)

    def _upload(self, parsed: object) -> None:
        query = parse_qs(parsed.query)  # type: ignore[attr-defined]
        filename = unquote(query.get("name", ["document.pdf"])[0])
        if Path(filename).suffix.casefold() != ".pdf":
            raise UIError("Please choose a PDF file.")
        length = int(self.headers.get("Content-Length", "0"))
        if length < 5 or length > MAX_UPLOAD_BYTES:
            raise UIError("The PDF is empty or exceeds the 4 GiB local upload limit.")
        upload_id = uuid.uuid4().hex[:16]
        target = UPLOAD_DIR / f"{upload_id}.pdf"
        temporary = target.with_suffix(".uploading")
        remaining = length
        with temporary.open("wb") as output:
            while remaining:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise UIError("The upload ended unexpectedly.")
                output.write(chunk)
                remaining -= len(chunk)
        with temporary.open("rb") as uploaded:
            if uploaded.read(5) != b"%PDF-":
                temporary.unlink(missing_ok=True)
                raise UIError("The selected file is not a valid PDF.")
        temporary.replace(target)
        try:
            count = pdf_page_count(target)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        self._json(
            {
                "upload_id": upload_id,
                "filename": filename,
                "suggested_name": safe_output_name(Path(filename).stem),
                "page_count": count,
            },
            HTTPStatus.CREATED,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local browser interface for Arabic OCR Batch")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    if not UI_HTML.is_file():
        print(f"error: interface file is missing: {UI_HTML}", file=sys.stderr)
        return 2
    address = f"http://{args.host}:{args.port}"
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Arabic OCR local interface: {address}")
    print("Keep this terminal open while using the interface. Press Ctrl+C to stop it.")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(address)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nInterface stopped. Completed OCR files remain saved.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
