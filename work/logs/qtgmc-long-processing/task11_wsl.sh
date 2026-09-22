#!/bin/bash
cd /mnt/e/vhs-upscale/worktrees/tape-qtgmc-long-processing
/mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna/.venv-rocm/bin/python -m pytest tests/test_run_local.py tests/test_manifest.py tests/test_chunks.py -v 2>&1
echo "EXIT_CODE=$?"
