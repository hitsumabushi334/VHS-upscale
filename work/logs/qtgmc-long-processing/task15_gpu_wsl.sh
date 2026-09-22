#!/bin/bash
cd /mnt/e/vhs-upscale/worktrees/tape-qtgmc-long-processing
P=/mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna/.venv-rocm/bin/python
echo "=== TASK15 WSL GPU SUITE (-m gpu) ==="
$P -m pytest tests --rootdir=/mnt/e/vhs-upscale/worktrees/tape-qtgmc-long-processing -m gpu -q 2>&1
echo "EXIT_CODE=$?"
