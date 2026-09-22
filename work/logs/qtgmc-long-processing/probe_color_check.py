"""Advisory verification: encode 3 frames with the EXACT Task 10 worker argv, ffprobe the result.

Question: does FFmpeg 8.1.1 (Gyan) FFV1/MKV omit color_primaries/color_transfer
while still writing color_range/color_space? Also: does -sar persist SAR?
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp(prefix="probe_color_"))
png_dir = tmp / "frames"
png_dir.mkdir()
subprocess.run(
    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
     "-f", "lavfi", "-i", "color=c=gray:s=8x8:r=30", "-frames:v", "3",
     str(png_dir / "%09d.png")],
    check=True,
)
partial = tmp / "chunk.mkv"
# Exact worker argv shape (pipeline/tape_chunk.py encode_chunk_ffv1):
subprocess.run(
    ["ffmpeg", "-hide_banner", "-loglevel", "error",
     "-y",
     "-framerate", "30",
     "-start_number", "1",
     "-i", str(png_dir / "%09d.png"),
     "-frames:v", "3",
     "-c:v", "ffv1",
     "-level", "3",
     "-g", "1",
     "-slicecrc", "1",
     "-pix_fmt", "yuv422p10le",
     "-color_range", "tv",
     "-colorspace", "bt709",
     "-color_primaries", "bt709",
     "-color_trc", "bt709",
     "-sar", "1:1",
     "-fps_mode", "cfr",
     "-an",
     str(partial)],
    check=True,
)
probe = json.loads(subprocess.run(
    ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(partial)],
    check=True, capture_output=True, text=True,
).stdout)["streams"][0]
keys = ("color_range", "color_space", "color_primaries", "color_transfer",
        "sample_aspect_ratio", "avg_frame_rate", "pix_fmt", "codec_name")
result = {k: probe.get(k, "<OMITTED>") for k in keys}
print(json.dumps(result, indent=2))
missing = [k for k in keys if probe.get(k) is None]
print("OMITTED:", missing)
sys.exit(0)
