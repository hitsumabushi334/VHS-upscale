#!/bin/bash
# DEPRECATED 2026-09-22: pre-restructure script; pipeline tests moved to the
# /mnt/e/vhs-upscale root. Use gate_root_wsl.sh for the canonical root gate.
# Kept as historical record only.
PY=/mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna/.venv-rocm/bin/python
$PY -c "import torch, numpy, PIL, cv2, clip, skimage; print('deps-ok', torch.__version__)"
ffmpeg -version 2>/dev/null | head -1
echo EXIT=$?

