# High-accuracy Kraken workflow

Use this workflow when Tesseract creates a searchable PDF but its Arabic text
is not accurate enough. It is CPU-intensive and intentionally separate from
the normal CLI.

## Requirements

- WSL2 Ubuntu
- Poppler (`pdftoppm`)
- Python 3.11+
- Kraken 7.1 or newer in an isolated virtual environment
- An OpenITI-compatible layout model
- A recognition model such as OpenITI AOCP or Kraken PP-OCRv6

Keep virtual environments and model files in the Linux filesystem for better
performance. Do not commit model weights; follow each model's license and
model card.

Model references:

- Kraken documentation: <https://kraken.re/main/>
- OpenITI Arabic Print Data: <https://github.com/OpenITI/arabic_print_data>
- PP-OCRv6 medium model: <https://zenodo.org/records/21788410>

## Generic single-model test

From the repository root, choose an ASCII job identifier and quote every path
that may contain spaces or Arabic characters:

```bash
mkdir -p work/my-book/pages
pdftoppm -r 300 -png "input/pilot/my-book.pdf" "work/my-book/pages/page"

python3 scripts/run_kraken_batch.py \
  --kraken /path/to/kraken \
  --pages work/my-book/pages \
  --output work/my-book/ppocrv6-alto \
  --layout-model /path/to/layout.mlmodel \
  --recognition-model /path/to/medium.safetensors \
  --threads 2 --line-batch-size 1 --workers 3

mapfile -t ALTO_FILES < <(
  find work/my-book/ppocrv6-alto -name "*.xml" -type f | sort
)

python3 scripts/kraken_alto_to_text.py \
  "${ALTO_FILES[@]}" \
  --output "output/kraken/my-book.txt"
```

Rerunning `run_kraken_batch.py` skips nonempty ALTO files, so an interrupted
run resumes missing pages.

## Ensemble workflow

Run the same page images through two recognizers using the same layout model.
Then pass the primary and fallback ALTO directories to:

```bash
python3 scripts/kraken_ensemble_to_text.py \
  --primary-dir work/my-book/ppocrv6-alto \
  --fallback-dir work/my-book/openiti-alto \
  --output output/kraken/my-book-hybrid.txt
```

The current correction table was developed for one theatre text and includes
character-name normalization. Review or replace `SPEAKER_NAMES`,
`NAME_ALIASES`, `INLINE_ALIASES`, and `TOKEN_ALIASES` before using the ensemble
on an unrelated work. For an initial test on a new book, the generic
single-model workflow is safer.

## Evaluation

Create exact UTF-8 transcriptions for representative pages. Preserve the
printed content; do not silently modernize spelling. Keep private/copyrighted
ground truth outside commits.

```bash
python3 scripts/evaluate_ocr.py \
  --candidate output/kraken/my-book.txt \
  --reference 10=/private/path/page-010.gt.txt \
  --reference 50=/private/path/page-050.gt.txt
```

The evaluator normalizes common Arabic/Persian code-point variants and reports
character error rate per page and overall. Compare models on the same held-out
pages before accepting a change.

## Concurrency

Start conservatively. On a four-core laptop, three workers with two requested
threads each worked reliably, but model architecture, RAM, cooling, and page
density affect the best setting. Larger recognition batches can be slower on
CPU. Completed ALTO pages remain safe if a run is interrupted.

