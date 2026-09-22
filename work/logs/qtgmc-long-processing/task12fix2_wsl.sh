#!/bin/bash
cd /mnt/e/vhs-upscale/worktrees/tape-qtgmc-long-processing
P=/mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna/.venv-rocm/bin/python
echo "=== GATE 1: long_media + short_inference ==="
$P -m pytest tests/test_long_media_contract.py tests/test_short_inference_contract.py -q 2>&1
echo "EXIT1=$?"
echo "=== GATE 2: run_local + manifest + chunks ==="
$P -m pytest tests/test_run_local.py tests/test_manifest.py tests/test_chunks.py -q 2>&1
echo "EXIT2=$?"
