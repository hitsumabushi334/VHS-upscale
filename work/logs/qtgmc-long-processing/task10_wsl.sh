#!/bin/bash
cd /mnt/e/vhs-upscale/worktrees/tape-qtgmc-long-processing
/mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna/.venv-rocm/bin/python -m pytest tests/test_tape_chunk.py tests/test_inference_contract.py tests/test_short_inference_contract.py -v 2>&1
echo "EXIT_CODE=$?"
