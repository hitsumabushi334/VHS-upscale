#!/bin/bash
# DEPRECATED 2026-09-22: pre-restructure script; pipeline tests moved to the
# /mnt/e/vhs-upscale root. Use gate_root_wsl.sh for the canonical root gate.
# Kept as historical record only.
cd /mnt/e/vhs-upscale/worktrees/tape-qtgmc-long-processing
/mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna/.venv-rocm/bin/python -m pytest tests/test_run_local.py tests/test_qtgmc_contract.py -m "not vapoursynth" -v 2>&1
echo "EXIT_CODE=$?"
