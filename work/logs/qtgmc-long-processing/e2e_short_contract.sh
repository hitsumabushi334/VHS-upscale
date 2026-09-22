#!/bin/bash
set -euxo pipefail
cd /mnt/e/vhs-upscale
P=/mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna/.venv-rocm/bin/python
export PYTORCH_ALLOC_CONF=expandable_segments:True
export PYTHONUNBUFFERED=1

# Next E2E after raw TAPE smoke:
# official short runner = TAPE + FFV1 encode + audio remux + report.json contract.
INPUT=input/VHS_10s_704x396.mkv
OUT=work/tmp/e2e-short-contract

rm -rf "$OUT"
mkdir -p "$OUT"
echo "=== START $(date) ==="
echo "NOTE: run_short_inference captures TAPE stdout; progress looks quiet until the end."

"$P" -u scripts/run_short_inference.py \
  --input "$INPUT" \
  --checkpoint work/assets/checkpoint.pth \
  --output "$OUT"

echo "=== EXIT=$? $(date) ==="
"$P" - <<'PY'
import json
from pathlib import Path
p = Path("work/tmp/e2e-short-contract") / "report.json"
d = json.loads(p.read_text(encoding="utf-8"))
print("status:", d.get("status"))
print("problems:", d.get("problems"))
results = d.get("results") or []
if results:
    r0 = results[0]
    print("final:", r0.get("final"))
    print("frame_count:", r0.get("frame_count"))
print("contract.passed:", (d.get("contract") or {}).get("passed"))
PY
ls -la "$OUT"
