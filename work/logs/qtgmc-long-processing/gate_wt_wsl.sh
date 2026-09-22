#!/bin/bash
cd /mnt/e/vhs-upscale/worktrees/tape-qtgmc-long-processing
P=/mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna/.venv-rocm/bin/python
echo "=== GATE: TAPE worktree suite (full) ==="
$P -m pytest tests/ -q 2>&1
echo "EXIT_CODE=$?"
