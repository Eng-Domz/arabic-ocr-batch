# Privacy and repository hygiene

Scanned books, OCR output, logs, model weights, and the SQLite state database
may contain copyrighted, confidential, or personally identifying data. They
must not be committed.

The `.gitignore` excludes:

- local `config.toml`;
- all input and output contents;
- work, temporary, log, report, and state contents;
- PDFs and rendered images;
- ALTO XML and OCR databases;
- Kraken model weights;
- private manually transcribed ground-truth fixtures.

Before every push, review the staged file list:

```bash
git status --short
git diff --cached --name-only
```

Do not use `git add -f` to bypass these protections. If sensitive data is ever
committed, removing it in a later commit is insufficient because it remains in
history; rotate any exposed credentials and rewrite history before sharing.

