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
5. Choose a quality mode:
   - **Fast** uses PP-OCRv6 only and is intended for drafts.
   - **Smart** runs PP-OCRv6 on every page and sends risky pages to Surya.
   - **Best Quality** uses Surya for every page and PP-OCRv6 as a second
     opinion. This is the recommended mode for final text.
6. Optionally enable **Also create a searchable PDF**.
7. Choose a **CPU load**. Balanced is recommended; Gentle keeps the laptop
   more responsive, while Faster uses more CPU and memory.
8. Optionally click **Enable finish notification** and allow browser
   notifications.
9. Click **Start OCR** and leave the terminal open until processing finishes.

Start with a small representative range, such as 3–5 pages, before processing
a complete book. On the prepared laptop, Best Quality measured approximately
13 minutes for five pages, or about 4 hours 20 minutes for 100 pages before
manual review and retries. The interface shows an estimate before the run.

## Understand the outputs

Every interface job is saved in a separate folder below:

```text
output/ui/<job-id>/
```

The interface provides download buttons for:

- **High-accuracy text**: UTF-8 TXT from the selected mode. Surya is the
  primary source for pages processed in Best Quality or Smart mode.
- **Uncertainty report**: JSON containing the displayed text, model
  disagreement labels, confidence signals, and the PP-OCR second opinion.
- **Searchable PDF**: optional PDF/A output whose pages can be searched and
  copied. Its hidden text layer is produced by OCRmyPDF/Tesseract and may not
  exactly match the high-accuracy TXT.

Uploaded working copies, rendered page images, OCR XML, reports, and outputs
are excluded from Git. The original PDF remains in its original location.

## Review uncertain text

When OCR finishes, the interface shows the scanned page beside the recognized
Arabic text.

- **Yellow — Models disagree**: Surya and PP-OCR produced different text.
- **Red — Strong mismatch**: one model omitted text or the disagreement is
  too large to resolve automatically.
- **No label**: both recognizers agreed after harmless Arabic spelling-form
  normalization, or only PP-OCR was run and its existing confidence gate did
  not flag the token.

Confidence is only one review-priority signal. It is never treated as proof:
our test data included incorrect PP-OCR words above 90% confidence. Expand
**Compare the complete PP-OCR second opinion** below a Surya page when a
highlight needs context. Proofread important names, numbers, and punctuation
against the scan even when both models agree.

Use **Previous/Next uncertain page** to move through pages needing attention,
**Show all pages** when a complete review is required, and the zoom controls to
inspect small print. Page-number headers are excluded from warning counts to
reduce noise.

## Correct and save reviewed text

After OCR completes, the **Correct and save text** area contains the complete
TXT output. Make corrections there and click **Save reviewed text**. The app
creates a separate `- reviewed.txt` download; it never overwrites the raw OCR
TXT.

## Resume or retry

If a job fails, open it under **Recent jobs** and select **Retry / resume**.
Already completed PP-OCR page files are reused. Surya work is saved in batches
of up to eight pages, so a completed batch is also reused after a retry.

After the computer or interface is restarted, previously interrupted jobs are
shown as needing attention and can be resumed from the same screen.

To stop a running job, click **Cancel safely**. The app stops all of that job's
OCR workers, marks it as cancelled, and keeps completed page results. Use
**Retry / resume** later to continue it.

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

### Smart and Best Quality are unavailable

Install the optional Surya engine once from Ubuntu:

```bash
cd /mnt/c/Users/Ram/Desktop/arabic-ocr-batch
bash scripts/install_surya_wsl.sh
```

The official Python package is large because its PyTorch dependency includes
GPU libraries even when this laptop uses the CPU backend. Restart the
interface after installation. The first Surya job also downloads the model
weights; later jobs reuse them. Override the detected executables with
`ARABIC_OCR_SURYA` and `ARABIC_OCR_LLAMA_SERVER` if installed elsewhere.

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

