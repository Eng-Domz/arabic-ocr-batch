# Arabic OCR Batch

A local, restart-safe batch pipeline for turning scanned Arabic or Arabic-English PDFs into searchable PDF/A files plus complete UTF-8 TXT files. It is intended for collections of roughly 100–500 documents and keeps the source PDFs untouched.

The restart-safe searchable-PDF pipeline uses only the Python 3.11+ standard library. OCR is performed by [OCRmyPDF](https://ocrmypdf.readthedocs.io/) and Tesseract; qpdf validates every completed PDF, then Poppler's `pdftotext` extracts the finished PDF's complete text layer before the PDF/TXT pair is published. For difficult Arabic print, the repository also includes an optional Kraken ensemble workflow that combines an OpenITI model's short-label recognition with PP-OCRv6 body text.

Additional documentation:

- [Architecture and safety invariants](docs/ARCHITECTURE.md)
- [High-accuracy Kraken workflow](docs/KRAKEN.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Privacy and repository hygiene](docs/PRIVACY.md)
- [Contributing](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)

No open-source license has been selected yet. The absence of a license means
the code is not automatically granted redistribution or modification rights.

## Local browser interface

The project includes a private local interface for the high-accuracy workflow.
It accepts a PDF copy, validates page ranges, runs PP-OCRv6, saves UTF-8 text,
and shows a page-by-page review screen. Words below 80% model confidence are
yellow; words below 60% are red. Confidence is a review aid, not a guarantee:
an unmarked word can still be wrong.

From WSL Ubuntu in the project folder:

```bash
PYTHONPATH=src python3 -m arabic_ocr_batch.web_ui
```

On Windows, you can instead double-click `start-ui.cmd` in the project folder.

Open <http://127.0.0.1:8765> if the browser does not open automatically. Keep
the Ubuntu terminal open during OCR. The interface supports page selections
such as `1-10,15,20-25`, live progress, retry/resume after a failed run,
browser completion notifications, downloadable high-accuracy TXT and review
JSON, and an optional searchable PDF. Outputs are saved below `output/ui/`;
uploaded working copies and rendered pages remain ignored by Git. The original
PDF selected in the browser is never changed.

The interface expects the model layout used by the documented Kraken setup:

```text
~/.local/share/arabic-ocr-batch/models/AOCP_print_models/layout/layout-20210711_AQ.mlmodel
~/.local/share/arabic-ocr-batch/models/ppocrv6/medium.safetensors
```

Override these with `ARABIC_OCR_LAYOUT_MODEL`,
`ARABIC_OCR_RECOGNITION_MODEL`, or `ARABIC_OCR_KRAKEN` when needed.

## Supported local setup

The intended Windows setup is WSL2 with Ubuntu and Python 3.11 or newer. Keep models and virtual environments in the Linux filesystem for speed, while input and published output may remain below the Windows project directory. Run `doctor` after installation to verify the actual machine rather than relying on a saved status in this README.

## Safety model

- Source PDFs are opened read-only. The pipeline never deletes, moves, or overwrites them.
- Output paths mirror each PDF's path below `input/`.
- OCR writes to a unique work directory. qpdf validates the result, then `pdftotext` extracts all text (including pages OCRmyPDF skipped because they already contained text) before publication.
- The extracted text is checked using Unicode letters and numbers per page. If `--skip-text` trusted a sparse or bogus existing layer, the default configuration automatically makes one bounded retry from the untouched source with `--redo-ocr`. Because OCRmyPDF does not allow `--redo-ocr` with `--deskew`, only that retry omits deskew. A retry that is still below the configured threshold fails without publishing the new PDF/TXT pair.
- The PDF/TXT pair is staged and published as a rollback-capable transaction. If either publication fails, both prior artifacts are restored (or both new artifacts are removed), so a retry cannot mix generations.
- Existing output is never replaced unless the SQLite database identifies that exact path as project-managed. Ownership survives a later failed retry without claiming unrelated files.
- Input, output, and work directory overlap is rejected.
- Case-insensitive output collisions are rejected, which is important on `/mnt/c`.
- SQLite stores source and processing-configuration fingerprints, statuses, attempts, and errors. A successful matching job with both outputs present is skipped on later runs.
- An atomically created project lock allows only one `run` command at a time. Only active worker slots are claimed, so waiting documents do not consume attempts.

## Install the system tools in WSL Ubuntu

First open PowerShell and check whether Ubuntu is already installed:

```powershell
wsl --status
wsl --list --verbose
```

If Ubuntu is not listed, run this once from an **Administrator PowerShell**, then reboot if Windows asks:

```powershell
wsl --install -d Ubuntu
```

Open Ubuntu and install the required packages:

```bash
sudo apt update
sudo apt install -y \
  python3 python3-venv \
  ocrmypdf \
  tesseract-ocr tesseract-ocr-ara tesseract-ocr-eng \
  ghostscript qpdf poppler-utils
```

Optional preprocessing tools (not required by the default configuration):

```bash
sudo apt install -y unpaper pngquant
```

Why these are needed: OCRmyPDF coordinates the pipeline, Tesseract performs Arabic/English recognition, Ghostscript creates PDF/A output, qpdf validates and safely rewrites PDFs, and Poppler extracts the complete UTF-8 text from the finished searchable PDF.

## Open and check the project from WSL

The Windows Desktop is mounted under `/mnt/c`:

```bash
cd /mnt/c/Users/<WindowsUser>/Desktop/arabic-ocr-batch
PYTHONPATH=src python3 -m arabic_ocr_batch doctor
```

`doctor` checks all required executables, the configured `ara` and `eng` Tesseract models, paths, and available disk space. Do not start a large run until it passes.

The ready-to-run local `config.toml` is ignored by Git. `config.example.toml` is the publishable template. To restore the local config later:

```bash
cp config.example.toml config.toml
```

## Test with 3–5 representative PDFs first

1. **Copy** (do not move) 3–5 representative PDFs into `input/`. Include poor scans, rotated pages, Arabic-only material, and mixed Arabic-English material if those occur in the collection. Nested folders are fine.

   Do not commit pilot documents. The repository ignore rules exclude PDFs, rendered pages, OCR output, model weights, logs, and state databases.
2. Run the checks and sample batch:

   ```bash
   PYTHONPATH=src python3 -m arabic_ocr_batch doctor
   PYTHONPATH=src python3 -m arabic_ocr_batch run --limit 5
   ```

3. Inspect the matching files below `output/searchable/` and `output/text/`. Check Arabic letter recognition, page rotation, numbers, punctuation, mixed English, and reading order.
4. Adjust `config.toml` if needed. Any processing-setting change gets a new configuration fingerprint, so documents are safely queued again. Existing project-managed outputs may then be replaced only after the new results validate.

The default load is deliberately conservative: one document at a time and two Tesseract jobs inside that document. Increase `document_workers` only after observing CPU temperature, memory, and responsiveness. Total OCR concurrency is approximately `document_workers × ocr_jobs`.

With `--skip-text`, OCRmyPDF does not OCR pages that already contain text. The pipeline deliberately does not publish OCRmyPDF's partial sidecar; it runs `pdftotext` on the finished searchable PDF so the TXT includes both existing and newly recognized text. It then counts Unicode alphanumeric characters and divides by the page count represented by `pdftotext` page separators. If that result is below `text_quality.minimum_alphanumeric_chars_per_page` (10 by default), `text_quality.automatic_redo` makes one `--redo-ocr` attempt. This language-neutral check works for Arabic, English, and mixed documents. Disable the retry only if sparse pages are expected and should be accepted as-is.

## Higher-accuracy Kraken ensemble

Tesseract is convenient for searchable PDFs but may be inaccurate on older Arabic typefaces. The optional advanced workflow uses the same page segmentation twice:

1. OpenITI AOCP recognition supplies short speaker names and marginal labels.
2. Kraken's PP-OCRv6 medium multilingual model supplies normal dialogue and prose.
3. `scripts/kraken_ensemble_to_text.py` matches lines by coordinates, restores right-to-left row order, normalizes recurring speaker-name confusions, and drops scan-watermark/page-number noise.

Render pages at 300 DPI, then create the two ALTO directories with the same layout model. The batch runner is restart-safe at page level:

```bash
python3 scripts/run_kraken_batch.py \
  --kraken /path/to/kraken \
  --pages work/book/pages \
  --output work/book/openiti-alto \
  --layout-model /path/to/layout.mlmodel \
  --recognition-model /path/to/openiti.mlmodel \
  --threads 2 --line-batch-size 1 --workers 3

python3 scripts/run_kraken_batch.py \
  --kraken /path/to/kraken \
  --pages work/book/pages \
  --output work/book/ppocrv6-alto \
  --layout-model /path/to/layout.mlmodel \
  --recognition-model /path/to/medium.safetensors \
  --threads 2 --line-batch-size 1 --workers 3

python3 scripts/kraken_ensemble_to_text.py \
  --primary-dir work/book/ppocrv6-alto \
  --fallback-dir work/book/openiti-alto \
  --output output/kraken/book-hybrid.txt
```

Do not assume that a newer model is better for a particular scan. Create manually verified page fixtures and measure character error rate before replacing an accepted result:

```bash
python3 scripts/evaluate_ocr.py \
  --candidate output/kraken/book-hybrid.txt \
  --reference 25=tests/fixtures/page-025.gt.txt
```

The ensemble script does not invent missing prose and does not use a language model to silently rewrite the source. Its small proper-name correction table is deliberately visible in the script and should be adapted for another book.

## Run the full collection

After approving the sample quality, copy the rest of the collection below `input/` and run:

```bash
PYTHONPATH=src python3 -m arabic_ocr_batch run
```

Useful commands:

```bash
# Show durable state counts
PYTHONPATH=src python3 -m arabic_ocr_batch status

# Export and print current failures
PYTHONPATH=src python3 -m arabic_ocr_batch failures

# Explicitly retry jobs that exhausted their attempts
PYTHONPATH=src python3 -m arabic_ocr_batch run --retry-failed
```

A normal failure remains retryable until `max_attempts` is reached. `--retry-failed` resets failed counters explicitly. Pressing Ctrl-C stops active OCR process groups, leaves unfinished items retryable, and releases the project lock after active workers stop.

An abrupt process or machine crash can leave `state/ocr-state.sqlite3.run.lock` behind. A later run will refuse to start and display its PID/host/start metadata. First confirm that no OCR run for this project is active, then recover explicitly:

```bash
PYTHONPATH=src python3 -m arabic_ocr_batch run --recover-lock
```

The recovered run has the exclusive lock before it changes any `running` records, so it cannot reset a live job during normal operation. Never use `--recover-lock` to bypass a run that is still active.

## Outputs and runtime data

```text
input/                  original PDFs (never modified)
output/searchable/      mirrored searchable PDF/PDF-A outputs
output/text/            mirrored complete UTF-8 TXT files
logs/                   timestamped detailed run logs
reports/failures.csv    current failed jobs (UTF-8 with BOM for Excel)
state/ocr-state.sqlite3 restart-safe job state
state/*.run.lock        single-run lock (present only while running, or after a crash)
work/jobs/              temporary per-document data, cleaned after each job
```

For example, `input/books/كتاب.pdf` becomes:

```text
output/searchable/books/كتاب.pdf
output/text/books/كتاب.txt
```

## Configuration notes

The defaults enable `--rotate-pages`, `--deskew`, `--skip-text`, PDF/A output, optimization level 1, `ara+eng`, and the bounded low-text-quality retry described above. Change `languages` to `"ara"` when English recognition is unnecessary. `extra_args` accepts additional compatible OCRmyPDF flags, but output-controlling options managed by the pipeline are rejected. Output fingerprints include OCR settings, text-quality settings, and the extraction-pipeline revision; changing worker count, OCR job count, timeout, or attempt policy does not needlessly regenerate successful documents. This revision causes previously completed documents to be processed once with the new quality check, then safely skipped again on later runs.

The program invokes tools with argument arrays and never through a shell. `tools.ocrmypdf` runs OCR, `tools.qpdf` validates the staged PDF, and `tools.pdftotext` extracts its full UTF-8 text layer. The `tesseract`, `ghostscript`, and `pdfinfo` entries are **doctor-only probes**; OCRmyPDF locates its own Tesseract and Ghostscript executables from the WSL `PATH`. Changing those probe entries does not redirect OCRmyPDF. Tool entries may be strings or TOML arrays; arrays are useful for wrappers and offline testing. The output fingerprint includes an explicit text-pipeline revision and the configured `pdftotext` command, so output made by the older sidecar pipeline is regenerated once and then resumes normal restart-safe skipping. `--sidecar` remains reserved and cannot be supplied through `ocr.extra_args`.

## Run the offline tests

The test suite uses fake OCR, qpdf, and pdftotext commands, so it needs no OCR installation:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Publishing later

The runtime data and local `config.toml` are ignored by Git. Before publishing, choose a license and add it deliberately; this project does not assume one. Do not commit scanned source documents, OCR outputs, logs, reports, or the SQLite database.

