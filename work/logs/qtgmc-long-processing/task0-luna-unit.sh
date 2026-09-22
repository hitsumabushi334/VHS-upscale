#!/bin/bash
# DEPRECATED 2026-09-22: pre-restructure script; pipeline tests moved to the
# /mnt/e/vhs-upscale root. Use gate_root_wsl.sh for the canonical root gate.
# Kept as historical record only.
cd /mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna || exit 2
.venv-rocm/bin/python -m pytest tests --rootdir=. -m 'not gpu and not integration' -q > /mnt/e/vhs-upscale/work/logs/qtgmc-long-processing/task0-luna-venv-unit.log 2>&1
echo "pytest_exit=$?"
