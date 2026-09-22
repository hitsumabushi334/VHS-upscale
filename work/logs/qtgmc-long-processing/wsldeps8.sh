#!/bin/bash
PY=/mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna/.venv-rocm/bin/python
$PY -c "import torch, numpy, PIL, cv2, clip, skimage; print('deps-ok', torch.__version__)"
ffmpeg -version 2>/dev/null | head -1
echo EXIT=$?

