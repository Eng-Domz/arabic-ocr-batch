# Contributing

1. Use Python 3.11 or newer.
2. Keep source PDFs, OCR output, logs, state, models, and private ground truth
   outside commits.
3. Make focused changes and add tests for behavior changes.
4. Run:

   ```bash
   PYTHONPATH=src python3 -m unittest discover -s tests -v
   python3 -m py_compile scripts/*.py src/arabic_ocr_batch/*.py
   ```

5. Document user-visible configuration or workflow changes.

Do not add a dependency merely for convenience; the packaged batch pipeline
currently uses the Python standard library. External OCR tools are runtime
requirements, not Python package dependencies.

No license has been selected yet. Contributions should not include copied book
text, model weights, or third-party assets whose redistribution is unclear.

