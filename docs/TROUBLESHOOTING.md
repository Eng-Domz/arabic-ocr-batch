# Troubleshooting

## `--config` is rejected

`--config` is a global option and must appear before the subcommand:

```bash
PYTHONPATH=src python3 -m arabic_ocr_batch --config config.toml doctor
```

## Sidecar says OCR was skipped

Some PDFs contain a sparse or bogus existing text layer. The normal pipeline
extracts text from the completed PDF and measures text density. With
`text_quality.automatic_redo = true`, it makes one bounded `--redo-ocr`
attempt from the untouched source. OCRmyPDF does not permit `--deskew` together
with `--redo-ocr`, so the retry omits deskew.

## A batch stopped or the terminal closed

Run the same command again. Successful matching jobs are skipped. Kraken page
runs also skip existing nonempty ALTO files.

If the normal CLI reports a stale run lock, first make certain the prior run is
not active. Only then use:

```bash
PYTHONPATH=src python3 -m arabic_ocr_batch run --recover-lock
```

Recovery refuses to replace a lock owned by a live process.

## Arabic filenames display incorrectly

The program writes UTF-8 and configures terminal error handling defensively.
Use a UTF-8 capable terminal and quote paths. Windows Notepad should show the
generated UTF-8 text correctly.

## Kraken is slow

- Keep the Python environment and model files under the WSL Linux filesystem.
- Keep rendered pages and results in the project if Windows access is needed.
- Use conservative workers; excessive workers can increase contention.
- Do not increase line batch size blindly on CPU.
- Resume instead of deleting completed ALTO pages.

## Text order is wrong

Plain Kraken serialization can place marginal speaker labels after body text.
Use `kraken_alto_to_text.py`, which reconstructs rows from line coordinates
and orders fragments right-to-left.

## Searchable PDF text differs from Kraken text

They are separate outputs. OCRmyPDF/Tesseract creates the searchable PDF text
layer. Kraken scripts produce ALTO and UTF-8 text. This repository does not yet
graft the Kraken ensemble back into a PDF layer.

## Tests cannot import the package

Run tests from the repository root with `src` on the Python path:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

