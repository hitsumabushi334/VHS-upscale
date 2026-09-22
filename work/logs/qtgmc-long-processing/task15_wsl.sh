#!/bin/bash
# DEPRECATED 2026-09-22: pre-restructure script; pipeline tests moved to the
# /mnt/e/vhs-upscale root. Use gate_root_wsl.sh for the canonical root gate.
# Kept as historical record only.
cd /mnt/e/vhs-upscale/worktrees/tape-qtgmc-long-processing
P=/mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna/.venv-rocm/bin/python
echo "=== TASK15 WSL FULL SUITE ==="
$P -m pytest tests/ -q 2>&1
echo "EXIT_CODE=$?"
