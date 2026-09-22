#!/bin/bash
# DEPRECATED 2026-09-22: pre-restructure script; pipeline tests moved to the
# /mnt/e/vhs-upscale root. Use gate_root_wsl.sh for the canonical root gate.
# Kept as historical record only.
set -u
NEW=/mnt/e/vhs-upscale/worktrees/tape-qtgmc-long-processing
PY=/mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna/.venv-rocm/bin/python
LOG=/mnt/e/vhs-upscale/work/logs/qtgmc-long-processing
cd "$NEW" || exit 2
"$PY" -m pytest tests --rootdir=. -m 'not vapoursynth and not gpu and not integration and not e2e' -v > "$LOG/task0-newworktree-unit.log" 2>&1
echo "unit_exit=$?"
"$PY" -m pytest tests --rootdir=. -m 'integration' -v > "$LOG/task0-newworktree-integration.log" 2>&1
echo "integration_exit=$?"
"$PY" -m pytest tests --rootdir=. -m 'gpu' -v > "$LOG/task0-newworktree-gpu.log" 2>&1
echo "gpu_exit=$?"
