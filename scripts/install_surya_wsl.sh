#!/usr/bin/env bash
set -euo pipefail

engine_root="${XDG_DATA_HOME:-$HOME/.local/share}/arabic-ocr-batch/experiments/surya2"
uv_binary="${UV_BINARY:-$HOME/.local/bin/uv}"
llama_release="b11417"

if [[ ! -x "$uv_binary" ]]; then
  echo "uv is required at $uv_binary" >&2
  echo "Install uv first: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 2
fi

mkdir -p "$engine_root/llama"
if [[ ! -x "$engine_root/llama/llama-server" ]]; then
  archive="$engine_root/llama-${llama_release}.tar.gz"
  curl -fL --retry 3 \
    "https://github.com/ggml-org/llama.cpp/releases/download/${llama_release}/llama-${llama_release}-bin-ubuntu-x64.tar.gz" \
    -o "$archive"
  tar -xzf "$archive" -C "$engine_root/llama" --strip-components=1
fi

if [[ ! -x "$engine_root/.venv/bin/surya_ocr" ]]; then
  "$uv_binary" venv --python 3.11 "$engine_root/.venv"
  "$uv_binary" pip install --python "$engine_root/.venv/bin/python" "surya-ocr==0.22.1"
fi

"$engine_root/llama/llama-server" --version
"$engine_root/.venv/bin/surya_ocr" --help >/dev/null
echo "Surya is ready. Restart Arabic OCR Studio and choose Smart or Best Quality."
