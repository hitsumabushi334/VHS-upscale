#!/bin/bash
set -u
cd /mnt/e/vhs-upscale/worktrees/tape-qtgmc-long-processing
/mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna/.venv-rocm/bin/python -m pytest -m "not vapoursynth" tests/test_qtgmc_contract.py -v
