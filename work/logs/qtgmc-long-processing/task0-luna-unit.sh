#!/bin/bash
cd /mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna || exit 2
.venv-rocm/bin/python -m pytest tests --rootdir=. -m 'not gpu and not integration' -q > /mnt/e/vhs-upscale/work/logs/qtgmc-long-processing/task0-luna-venv-unit.log 2>&1
echo "pytest_exit=$?"
