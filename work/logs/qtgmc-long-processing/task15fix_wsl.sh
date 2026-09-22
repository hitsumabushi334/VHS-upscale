#!/bin/bash
cd /mnt/e/vhs-upscale/worktrees/tape-qtgmc-long-processing
P=/mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna/.venv-rocm/bin/python
echo "=== TASK15F WSL GATE (post-F1-fix) ==="
$P -m pytest tests/test_run_local.py tests/test_tape_chunk.py tests/test_manifest.py tests/test_long_media_contract.py tests/test_short_inference_contract.py -q 2>&1
echo "EXIT_CODE=$?"
