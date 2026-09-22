#!/bin/bash
cd /mnt/e/vhs-upscale/worktrees/tape-qtgmc-long-processing
/mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna/.venv-rocm/bin/python -m pytest tests/test_reference_cache.py tests/test_real_world_helpers.py tests/test_inference_contract.py -v 2>&1
echo "EXIT_CODE=$?"

