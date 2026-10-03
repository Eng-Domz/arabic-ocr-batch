# How to Use Arabic OCR Batch

This guide describes the simplest local workflow on Windows using the browser
interface. All OCR runs on your computer through WSL2 Ubuntu. Files are not
sent to an online OCR service, and the selected source PDF is never modified.

## Start the interface

1. Open `C:\Users\Ram\Desktop\arabic-ocr-batch` in File Explorer.
2. Double-click `start-ui.cmd`.
3. Keep the terminal window open while using the application.
4. If the browser does not open automatically, visit
   <http://127.0.0.1:8765>.

To start it manually from Ubuntu instead:

```bash
cd /mnt/c/Users/Ram/Desktop/arabic-ocr-batch
PYTHONPATH=src python3 -m arabic_ocr_batch.web_ui
```

Stop the interface by pressing `Ctrl+C` in its terminal. Completed outputs
remain saved.

## Process a PDF

1. Click **Choose File** and select a PDF.
2. Wait for the interface to show the filename and page count.
3. In **Pages**, enter one of the following:
   - `all` to process the entire document;
   - `1-10` to process one continuous range;
   - `1-10,15,20-25` to combine ranges and individual pages.
4. Change **Output name** if desired. Do not add a folder path.
5. Optionally enable **Also create a searchable PDF**.
6. Optionally click **Enable finish notification** and allow browser
   notifications.
7. Click **Start OCR** and leave the terminal open until processing finishes.

Start with a small representative range, such as 3–5 pages, before processing
a complete book. High-accuracy OCR is CPU-intensive and may take several
minutes per group of pages.

## Understand the outputs

Every interface job is saved in a separate folder below:

```text
output/ui/<job-id>/
```

The interface provides download buttons for:

- **High-accuracy text**: UTF-8 TXT produced from the PP-OCRv6 Arabic OCR
  result.
- **Confidence report**: JSON containing each recognized token and its model
  confidence.
- **Searchable PDF**: optional PDF/A output whose pages can be searched and
  copied. Its hidden text layer is produced by OCRmyPDF/Tesseract and may not
  exactly match the high-accuracy TXT.

Uploaded working copies, rendered page images, OCR XML, reports, and outputs
are excluded from Git. The original PDF remains in its original location.

## Review uncertain text

When OCR finishes, the interface shows the scanned page beside the recognized
Arabic text.

- **Yellow — Check**: model confidence is below 80%.
- **Red — Urgent review**: model confidence is below 60%.
- **No label**: the model reported at least 80% confidence.

Confidence is only a review-priority signal. A high-confidence word can still
be incorrect, especially with old fonts, damaged scans, punctuation, names,
page numbers, and similar Arabic letter shapes. Proofread important text
against the page image even when there are no red labels.

## Resume or retry

If a job fails, open it under **Recent jobs** and select **Retry / resume**.
Already completed OCR page files are reused, so the high-accuracy recognition
stage does not normally start from the beginning.

After the computer or interface is restarted, previously interrupted jobs are
shown as needing attention and can be resumed from the same screen.

## Process a large collection

Use the restart-safe command-line batch after representative samples have been
approved:

```bash
cd /mnt/c/Users/Ram/Desktop/arabic-ocr-batch
PYTHONPATH=src python3 -m arabic_ocr_batch --config config.toml doctor
PYTHONPATH=src python3 -m arabic_ocr_batch --config config.toml run
```

Put copied input PDFs below `input/`. The command-line batch saves searchable
PDFs below `output/searchable/` and complete TXT files below `output/text/`.
It tracks progress in SQLite and skips successfully completed documents when
run again.

The global `--config` option must appear before the command. For example,
`--config config.toml run` is correct; `run --config config.toml` is not.

## Common problems

### The page does not open

Confirm that the launcher terminal still says:

```text
Arabic OCR local interface: http://127.0.0.1:8765
```

Then open that address manually. If another copy is already running, use the
existing browser page or stop the older terminal with `Ctrl+C`.

### The interface says a model or Kraken is missing

The expected files on the prepared laptop are:

```text
~/.local/share/arabic-ocr-batch/models/AOCP_print_models/layout/layout-20210711_AQ.mlmodel
~/.local/share/arabic-ocr-batch/models/ppocrv6/medium.safetensors
```

See [docs/KRAKEN.md](docs/KRAKEN.md) for the advanced setup and override
environment variables.

### OCR appears frozen

Arabic recognition can spend significant time on one dense page. Check the
progress message and the launcher terminal. Do not start a second job while
one is running. Completed pages remain available for a retry if processing is
interrupted.

### Text is readable but not accurate enough

Review the highlighted words first, then compare names, numbers, punctuation,
and paragraph order against the scan. Use several manually corrected pages as
ground truth and measure changes with `scripts/evaluate_ocr.py` before changing
models or preprocessing settings.

More diagnostic guidance is available in
[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

