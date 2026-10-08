#!/usr/bin/env bash
set -euo pipefail

engine_root="${XDG_DATA_HOME:-$HOME/.local/share}/arabic-ocr-batch/experiments/surya2"
uv_binary="${UV_BINARY:-$HOME/.local/bin/uv}"
llama_release="b11417"
llama_cuda_version="12.8"

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

cuda_server="$engine_root/llama-cuda/llama-server"
cuda_available=false
if [[ "$(uname -m)" == "x86_64" ]] && command -v nvidia-smi >/dev/null 2>&1; then
  if nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | grep -q '[^[:space:]]'; then
    cuda_available=true
  fi
fi

install_cuda_llama() {
  local staging archive runtime_archive release_url
  staging="$(mktemp -d "$engine_root/llama-cuda.installing.XXXXXX")"
  archive="$staging/llama-cuda.tar.gz"
  runtime_archive="$staging/cudart.tar.gz"
  release_url="https://github.com/ggml-org/llama.cpp/releases/download/${llama_release}"
  if ! curl -fL --retry 3 \
      "$release_url/llama-${llama_release}-bin-ubuntu-cuda-${llama_cuda_version}-x64.tar.gz" \
      -o "$archive" || \
     ! curl -fL --retry 3 \
      "$release_url/cudart-llama-${llama_release}-bin-ubuntu-cuda-${llama_cuda_version}-x64.tar.gz" \
      -o "$runtime_archive" || \
     ! tar -xzf "$archive" -C "$staging" --strip-components=1 || \
     ! tar -xzf "$runtime_archive" -C "$staging" --strip-components=1; then
    rm -rf -- "$staging"
    return 1
  fi
  rm -f -- "$archive" "$runtime_archive"
  mv -- "$staging" "$engine_root/llama-cuda"
}

if [[ "$cuda_available" == true && ! -e "$engine_root/llama-cuda" ]]; then
  echo "NVIDIA GPU detected; installing the optional CUDA llama.cpp backend."
  if ! install_cuda_llama; then
    echo "CUDA installation failed; the CPU backend remains available." >&2
  fi
fi

if [[ ! -x "$engine_root/.venv/bin/surya_ocr" ]]; then
  "$uv_binary" venv --python 3.11 "$engine_root/.venv"
  "$uv_binary" pip install --python "$engine_root/.venv/bin/python" "surya-ocr==0.22.1"
fi

"$engine_root/llama/llama-server" --version
if [[ -x "$cuda_server" ]]; then
  if "$cuda_server" --list-devices 2>&1 | grep -qi cuda; then
    echo "CUDA llama.cpp backend is ready."
  else
    echo "CUDA backend is installed but unavailable; Arabic OCR Studio will use CPU." >&2
  fi
elif [[ "$cuda_available" == true ]]; then
  echo "CUDA backend is unavailable; Arabic OCR Studio will use CPU." >&2
fi
"$engine_root/.venv/bin/surya_ocr" --help >/dev/null
echo "Surya is ready. Restart Arabic OCR Studio and choose Smart or Best Quality."
