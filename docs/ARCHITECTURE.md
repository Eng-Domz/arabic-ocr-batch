# Architecture

Arabic OCR Batch contains two complementary local workflows.

## Searchable-PDF batch pipeline

The packaged `arabic_ocr_batch` CLI discovers PDFs recursively below the
configured input directory and maps each relative input path to one searchable
PDF and one UTF-8 text file.

```text
input PDF
  -> discovery and SHA-256 source fingerprint
  -> durable SQLite job record
  -> OCRmyPDF + Tesseract in a unique staging directory
  -> qpdf structural validation
  -> pdftotext extraction and per-page text-density check
  -> optional bounded --redo-ocr retry
  -> rollback-capable publication of the PDF/TXT pair
```

`config.py` parses TOML, resolves paths relative to the configuration file,
rejects unsafe path overlap, validates managed OCR options, and creates a
processing fingerprint. Scheduling-only settings do not invalidate good
outputs.

`discovery.py` walks the input tree without following symlinks, detects
case-insensitive output collisions, and fingerprints source documents.

`state.py` stores durable job status in SQLite and implements an atomic
cross-process run lock suitable for a Windows-mounted WSL directory.

`pipeline.py` owns subprocess lifetime, conservative parallelism, retry
behavior, staging, validation, rollback, interruption handling, and summary
counts. Source PDFs are only opened for reading.

`cli.py` exposes `doctor`, `run`, `status`, and `failures`. Global options such
as `--config` must appear before the subcommand.

## Kraken ALTO workflow

The scripts directory provides a separate high-accuracy text workflow for
difficult Arabic print:

- `run_kraken_batch.py` recognizes missing rendered pages and is restart-safe
  at page granularity.
- `kraken_alto_to_text.py` reconstructs top-to-bottom, right-to-left text from
  ALTO coordinates.
- `kraken_ensemble_to_text.py` combines a primary recognizer for prose with a
  fallback recognizer for short marginal labels.
- `evaluate_ocr.py` computes normalized Arabic character error rate against
  manually verified pages.

The Kraken workflow currently produces text and ALTO, not a replacement PDF
text layer. Use the OCRmyPDF workflow when searchable PDF/A is the primary
requirement.

## Safety invariants

- Input files are never overwritten, moved, or deleted.
- Output and input roots may not overlap.
- Existing unmanaged outputs are never replaced.
- A PDF/TXT generation is published together or rolled back together.
- Only active worker slots increment attempt counters.
- A live run lock cannot be bypassed with recovery mode.
- Runtime data and documents are excluded from version control.

