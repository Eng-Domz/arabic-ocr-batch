# Changelog

## Unreleased

## 0.2.0 - 2026-10-03

- Added safe in-app cancellation that stops the complete OCR process group while
  preserving finished page results for resume.
- Added gentle, balanced, and faster CPU worker settings plus live OCR ETA.
- Added uncertain-page navigation, scan zoom, and optional display of all pages.
- Added an editable text workspace that saves a separate reviewed TXT without
  modifying the original OCR result.
- Reduced confidence noise by excluding detected page-number headers from
  review counts.
- Improved cancelled-job state, retry behavior, and completion notifications.

## 0.1.0 - 2026-10-02

- Added restart-safe OCRmyPDF/Tesseract batch processing for Arabic and mixed
  Arabic-English PDFs.
- Added safe path validation, configuration fingerprints, durable SQLite
  state, exclusive run locking, bounded retries, failure reports, and atomic
  PDF/TXT publication.
- Added full-text extraction with a text-density quality gate and bounded
  `--redo-ocr` recovery.
- Added Kraken ALTO batch recognition, right-to-left coordinate reconstruction,
  optional two-model ensemble output, and character-error evaluation.
- Added architecture, Kraken, troubleshooting, privacy, and contribution
  documentation.

