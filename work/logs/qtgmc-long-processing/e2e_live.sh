#!/bin/bash
set -euxo pipefail
cd /mnt/e/vhs-upscale
P=/mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna/.venv-rocm/bin/python
export PYTORCH_ALLOC_CONF=expandable_segments:True
export PYTHONUNBUFFERED=1

# NOTE:
# - Do NOT rewrite this file via PowerShell Set-Content here-strings.
#   PowerShell wraps long lines and breaks the ffprobe input path onto the next line.
# - VHS_10s_ready.mkv (1280x720) + preprocess none OOMs (~10.99 GiB).
#   Use the smaller 704x396 clip for a full-pipeline smoke.
INPUT=input/VHS_10s_704x396.mkv
OUT=work/tmp/e2e-manual
STEM=$(basename "$INPUT" .mkv)

rm -rf "$OUT"
mkdir -p "$OUT"

FRAMES=$(ffprobe -v error -count_frames -select_streams v:0 -show_entries stream=nb_read_frames -of default=nw=1:nk=1 "$INPUT")
echo "INPUT=$INPUT"
echo "FRAMES=$FRAMES"
echo "=== START $(date) ==="

"$P" -u worktrees/tape-qtgmc-long-processing/real_world_inference.py \
  --input-path /mnt/e/vhs-upscale/"$INPUT" \
  --output-path /mnt/e/vhs-upscale/"$OUT" \
  --checkpoint-path /mnt/e/vhs-upscale/work/assets/checkpoint.pth \
  --num-input-frames 5 \
  --num-reference-frames 5 \
  --preprocess-mode none \
  --patch-size 512 \
  --frame-format png \
  --num-workers 1 \
  --expected-frame-count "$FRAMES"

echo "=== TAPE EXIT=$? $(date) ==="
echo "restored_frames=$(ls "$OUT/$STEM/restored_frames" | wc -l)"
