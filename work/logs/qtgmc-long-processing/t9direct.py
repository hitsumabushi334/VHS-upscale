import sys, types, json, tempfile
from pathlib import Path
import real_world_inference as rwi

# 1) imports without torch
print("import-ok torch-loaded:", "torch" in sys.modules)

# 2) parser: complete external set
args = rwi.build_parser().parse_args([
    "--input-path","s.mkv","--output-path","o",
    "--input-frames-path","ir","--reference-map-path","rm","--reference-frames-path","rf",
    "--skip-reference-selection","--global-start-frame","28",
])
print("parser-complete-ok input_frames:", args.input_frames_path)

# 3) partial set -> parser error
import pytest
for one in ["--input-frames-path x","--reference-map-path x","--reference-frames-path x","--skip-reference-selection","--global-start-frame 5"]:
    try:
        rwi.build_parser().parse_args(["--input-path","s.mkv","--output-path","o",one])
        print("partial-NOT-rejected:", one)
    except SystemExit:
        print("partial-rejected:", one)

