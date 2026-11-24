#!/bin/bash
# Install WanGP + MiniMax H3 (FL2VA pruned, int8) on a Linux NVIDIA node. Idempotent; safe to re-run.
#
#   bash worker/provision_wangp.sh            # clone, venv, deps, prefetch H3 weights
#   PREFETCH=0 bash worker/provision_wangp.sh # skip the prefetch; WanGP downloads on first use instead
#
# Env: WANGP_ROOT (~/Wan2GP), WANGP_VENV (~/venv-wangp), WANGP_COMMIT (the commit we used, below),
#      TORCH_INDEX (https://download.pytorch.org/whl/cu130 — pick the CUDA build that matches your driver).
# Tested on DGX Spark (GB10, arm64, CUDA 13, torch 2.14.1+cu130, Python 3.12).
set -euo pipefail
WANGP_ROOT=${WANGP_ROOT:-$HOME/Wan2GP}
WANGP_VENV=${WANGP_VENV:-$HOME/venv-wangp}
WANGP_COMMIT=${WANGP_COMMIT:-9d25c601f3c7074ca03c50f41b307a7c517835f9}
TORCH_INDEX=${TORCH_INDEX:-https://download.pytorch.org/whl/cu130}

[ -d "$WANGP_ROOT/.git" ] || git clone https://github.com/deepbeepmeep/Wan2GP.git "$WANGP_ROOT"
cd "$WANGP_ROOT"
git fetch -q origin && git checkout -q "$WANGP_COMMIT" || echo "WARN: could not pin $WANGP_COMMIT; using $(git rev-parse --short HEAD)"

[ -d "$WANGP_VENV" ] || python3 -m venv "$WANGP_VENV"
# shellcheck disable=SC1091
source "$WANGP_VENV/bin/activate"
python -c "import torch" 2>/dev/null || pip install torch torchvision torchaudio --index-url "$TORCH_INDEX"
python -c "import torch; print('torch', torch.__version__, 'cuda', torch.cuda.is_available())"

req=requirements.txt
if [ "$(uname -m)" = aarch64 ]; then
  # x86-only wheels that abort the install on arm64; H3 generation does not need them
  grep -vE "^(torchcodec|onnxruntime-gpu|insightface|rembg|decord|pycocotools|taichi|xatlas|fast-simplification|spacy_pkuseg|facexlib)" \
    requirements.txt > requirements.arm64.txt
  req=requirements.arm64.txt
fi
python -c "import diffusers, transformers" 2>/dev/null || pip install -r "$req"
python -c "import wgp" >/dev/null 2>&1 && echo "WGP import OK" || echo "WARN: 'import wgp' failed; see README troubleshooting"

if [ "${PREFETCH:-1}" = 1 ]; then
  # Same files WanGP fetches on first use; prefetching avoids a 40 GB download inside the first render.
  B=https://huggingface.co/DeepBeepMeep/MiniMax-H3/resolve/main
  mkdir -p ckpts/Qwen3-VL-32B-Instruct ckpts/minimax_h3
  dl() { [ -s "ckpts/$2" ] && { echo "have $2"; return; }; curl -fL -sS --retry 30 --retry-delay 5 -C - -o "ckpts/$2.part" "$B/$1" && mv "ckpts/$2.part" "ckpts/$2" && echo "got $2"; }
  dl MiniMax-H3-FL2VA-pruned_rank8_int8_convrot.safetensors MiniMax-H3-FL2VA-pruned_rank8_int8_convrot.safetensors &
  dl Qwen3-VL-32B-Instruct/qwen3vl-32B-MiniMax-H3-Q4_K_M.gguf Qwen3-VL-32B-Instruct/qwen3vl-32B-MiniMax-H3-Q4_K_M.gguf &
  dl minimax_h3/minimax_h3_video_vae_int8_convrot.safetensors minimax_h3/minimax_h3_video_vae_int8_convrot.safetensors &
  dl MiniMax-H3-audio_vae_fp32.safetensors MiniMax-H3-audio_vae_fp32.safetensors &
  dl MiniMax-H3-video_vae_fp16.safetensors MiniMax-H3-video_vae_fp16.safetensors &
  wait
fi
echo "PROVISION_DONE  (start the worker: tmux new -d -s sbq 'bash $(cd "$(dirname "$0")" && pwd)/qworker.sh ~/sbq')"
