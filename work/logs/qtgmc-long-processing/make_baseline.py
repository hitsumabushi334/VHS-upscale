import json

report = {
  "schema_version": 1,
  "recorded_at": "2026-09-20T08:46:28.432+00:00",
  "plan": "E:/vhs-upscale/docs/qtgmc-long-processing/implementation_plan.md",
  "worktree": "E:/vhs-upscale/worktrees/tape-qtgmc-long-processing",
  "branch": "codex/tape-qtgmc-long-processing",
  "head_commit": "c4ee9aae6e95ab257e3b3e4b538e6c20e1b7c9dc",
  "upstream_base": "fb97c608a1733dec5dbcd5aeb77ed84e29b793f7",
  "merge_base_check": "direct base (worktree created from c4ee9aae...)",
  "dirty_at_start": False,
  "windows": {
    "os": "Windows 10 Home 25H2",
    "python": "3.13.1 (C:/Python313/python.exe)",
    "vapoursynth": [79, 0],
    "vsjetpack": "2.2.4",
    "vspipe": "C:/Users/issho/AppData/Roaming/Python/Python313/Scripts/vspipe.exe",
    "ffmpeg": "C:/Users/issho/AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-8.1.1-full_build/bin/ffmpeg.exe",
    "ffprobe": "C:/Users/issho/AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-8.1.1-full_build/bin/ffprobe.exe",
    "wsl_distro": "Ubuntu-22.04 (running)"
  },
  "vapoursynth_probe": {
    "python_import_ok": True,
    "vsdeinterlace_import_ok": True,
    "qtempgaussmc_python_present": True,
    "bestsource_python_present": True,
    "synthetic_blankclip_first_frame": "ok",
    "qtgmc_first_frame_request": "FAIL (missing native plugin)",
    "qtgmc_first_frame_error": "No attribute with the name resize2 exists. Did you mistype a plugin namespace or forget to install a plugin?",
    "verdict": "Python packages present but native QTGMC plugins missing; Task 4 expected RED (missing_dependency)"
  },
  "assets": {
    "checkpoint": {"sha256": "14f89686d202a52bf14f0a18e9b7eadf617bf4c2425805a3d8ccba4351727a3d", "size": 378230179, "path": "E:/vhs-upscale/work/assets/checkpoint.pth"},
    "clip_rn50x4": {"sha256": "7e526bd135e493cef0776de27d5f42653e6b4c8bf9e0f653bb11773263205fdd", "size": 421854225, "path": "E:/vhs-upscale/work/assets/RN50x4.pt"},
    "swin_t": {"sha256": "704ceda373461b0a224fcdddd75cd2a5e9f8064512ed47adbddef7f343fd147b", "size": 113445839, "path": "E:/vhs-upscale/work/assets/swin_t.pt"},
    "verified_against": "E:/vhs-upscale/work/assets/assets.json (all 3 sha256/size match)"
  },
  "test_baseline": {
    "note": "New worktree has no vapoursynth/e2e markers yet; existing suite run with old markers via WSL venv Python (luna worktree venv, identical commit). GPU group uses the RX 7900 XT through the shared WSL environment.",
    "unit": {"command": "python -m pytest tests --rootdir=. -m 'not vapoursynth and not gpu and not integration and not e2e' -v", "result": "99 passed, 28 deselected", "log": "E:/vhs-upscale/work/logs/qtgmc-long-processing/task0-newworktree-unit.log"},
    "integration": {"command": "python -m pytest tests --rootdir=. -m 'integration' -v", "result": "25 passed, 102 deselected", "log": "E:/vhs-upscale/work/logs/qtgmc-long-processing/task0-newworktree-integration.log"},
    "gpu": {"command": "python -m pytest tests --rootdir=. -m 'gpu' -v", "result": "3 passed, 124 deselected", "log": "E:/vhs-upscale/work/logs/qtgmc-long-processing/task0-newworktree-gpu.log"},
    "unexpected_skips": 0
  },
  "known_constraints": {
    "tape_704x396": "297 frames in 282.633 s, 1.051 fps, peak allocated 8.81 GiB / reserved 14.56 GiB (AMP/autocast) from Task 8 of the TAPE ROCm plan",
    "tape_1280x720": "full-resolution TAPE fails closed with HIP OOM on this GPU",
    "vapoursynth": "native QTGMC plugins missing (resize2 namespace); preflight must fail closed until Task 4 install",
    "input": "input/ contains only progressive material; no 29.97i VHS source yet (Task 14/16 gate)"
  },
  "incident": {
    "name": "core.autocrlf CRLF checkout",
    "detail": "New worktree checkout converted LF tracked files to CRLF under global core.autocrlf=true; bash scripts became unparseable under WSL (64 test failures). Working tree rewritten byte-exact from git blobs (43 files), local core.autocrlf=false set in this worktree only.",
    "resolution": "verified: 0 mismatch vs HEAD blobs; full unit/integration/gpu groups PASS"
  }
}

out = "E:/vhs-upscale/work/local-pipeline/baseline.json"
with open(out, "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    f.write(chr(10))

d = json.load(open(out, encoding="utf-8"))
print("ok", d["head_commit"], d["test_baseline"]["unit"]["result"])

