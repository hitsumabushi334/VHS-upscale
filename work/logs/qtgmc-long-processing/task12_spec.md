# Task 12 spec: concat, audio remux, final verification (TC-33..TC-36)

Worktree: `E:/vhs-upscale/worktrees/tape-qtgmc-long-processing` (branch `codex/tape-qtgmc-long-processing`).
Host Python: `C:/Python313/python.exe` (3.13). Code MUST stay Python 3.10 compatible (the WSL gate runs 3.10).
Skip formatters, linters, and project-wide suites. Run only the targeted verification listed at the end.

## Goal

Implement plan Task 12 (`E:/vhs-upscale/docs/qtgmc-long-processing/implementation_plan.md` lines 844-870) and TC-33..TC-36 (`E:/vhs-upscale/docs/qtgmc-long-processing/test_cases.md` lines 275-303). Read both sections first, plus the existing code described below.

## Files (exclusive write scope)

- `pipeline/media_contract.py` (extend)
- `pipeline/run_local.py` (extend)
- `tests/test_long_media_contract.py` (extend)

Do NOT touch any other file. Do NOT commit (the coordinator commits after independent verification). Do not create new files except test fixtures you generate at runtime inside tmp dirs.

## Fixed contracts (coordinator-decided, do not re-decide)

### Chunk layout
- Real worker (`pipeline/tape_chunk.py:383-411`): `work_dir = work_root/tape/chunks/{index:06d}/`; published chunk = `work_dir/chunk.mkv`; sidecar = `work_dir/chunk.json`.
- Orchestrator (`pipeline/run_local.py:1754-1814`): the tape_chunks stage calls `runner(manifest, index, request)` and records `chunk_record = {index, status, hash, mtime_ns, ...}`; the artifacts list at ~line 1802-1805 currently assumes `work_root/chunks/chunk-{index:04d}.mkv`.
- UNIFIED RULE (implement this):
  1. In the orchestrator chunk loop, after a successful chunk run add `chunk_record["output_path"] = str(output_path)`.
  2. The artifacts list uses `Path(chunk_record["output_path"])` when present; otherwise falls back to the legacy `work_root/chunks/chunk-{index:04d}.mkv`.
  3. A chunk's sidecar is ALWAYS `Path(output_path).with_suffix(".json")`. This works for both layouts: `.../chunk.mkv -> .../chunk.json` and `chunks/chunk-0000.mkv -> chunks/chunk-0000.json`.

### Sidecar fields (already written by `tape_chunk._write_sidecar`, lines 357-377)
`schema_version, chunk_index, core_start, core_end, decode_start, decode_end, frame_hashes, reference_cache_key, checkpoint_sha256, ffmpeg{path,version}, cfr, avg_frame_rate, metrics, stderr_log, video{path,size,sha256}`.

### Existing manifest fields (read `pipeline/run_local.py` analyze_long_media ~lines 1200-1290)
- `versions.windows_ffmpeg_path`, `versions.windows_ffmpeg_version`, `versions.ffmpeg_cfr_policy`.
- `publish.{final_mkv, handoff_json, run_manifest_snapshot, chunk_manifest_snapshot, reference_manifest_snapshot, output_dir, reference_cache_root}`.
- Source frame count: `media_contract.build_long_manifest` stores `num_frames` (media_contract.py:436) - trace the dict nesting to find the exact path (it is under `manifest["source"]`).
- Reference cache manifest: `manifest["publish"]["reference_cache_root"] / "manifest.json"` (references.py:467).

### Design decisions (fixed)
1. Add `public_dir` to the `publish` config in `analyze_long_media`: default `str(Path(__file__).resolve().parents[1] / "work" / "tape")` (i.e. `E:\vhs-upscale\work\tape` for this repo). Tests MUST monkeypatch `publish.public_dir` to a tmp dir - tests never write to the real `work/tape`.
2. Final public name: `<safe_stem>-<source-hash12>_tape.mkv`.
   - `safe_stem`: source file stem used as-is iff it FULLY matches `^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$`, else `source-<hash12>`.
   - `hash12` = first 12 chars of the source file sha256 (same value used for work-root naming at run_local.py:2025, `_sha256_file(source)[:12]`).
3. Public artifacts (ALL atomic: write temp file then `os.replace`; never partial):
   - `public_dir/<safe_stem>-<hash12>_tape.mkv` - the final remuxed mkv (from `publish.final_mkv`), published only after verify PASS.
   - `public_dir/<safe_stem>-<hash12>_tape.json` - handoff JSON: final file name, sha256, duration, avg_frame_rate, video params (codec/pix_fmt/fps/width/height/SAR/DAR/color fields), audio streams (per-stream codec + packet count), the 8-bit warning text, source sha256, run_id.
   - `public_dir/<safe_stem>-<hash12>_run_manifest.json` - immutable copy of the DAG manifest.
   - `public_dir/<safe_stem>-<hash12>_chunk_manifest.json` - immutable copy of the tape_chunks stage record (chunk records incl. output_path/hash) .
   - `public_dir/<safe_stem>-<hash12>_reference_manifest.json` - immutable copy of the reference cache manifest.
   - The run-local side keeps the canonical DAG manifest, the verify report (`work_root/output/verify_report.json`), and all intermediate artifacts.
4. Real default runners, following the existing `_resolve_qtgmc_runner` pattern (run_local.py:1525-1554):
   - `_resolve_concat_runner(manifest, runner)` -> injected runner or the real concat implementation.
   - `_resolve_remux_runner(manifest, runner)` -> injected runner or the real remux implementation.
   - `_resolve_verify_runner(manifest, runner)` -> injected runner or the real verify implementation.
   Wire all three into the `executors` dict in `_run_pipeline` (run_local.py:1629-1636).
5. Extend the `verify(work_root)` CLI function (run_local.py:1988): after loading the DAG manifest, if the tape_chunks, concat, and remux stages are all complete, run the real verify implementation (final contract + publish), update the verify stage record, save the manifest, and return the report. Preserve the existing returned fields (`run_id`, `stages`, `contract_passed`, `problems`). If upstream stages are incomplete, return the current manifest-summary report without publishing.
6. Fail closed everywhere: any contract mismatch raises (RunLocalError in run_local / PreflightError-ValueError family in media_contract - follow each file's existing convention). No partial publish. Publish only after ALL checks pass.

## Required behavior (plan Task 12 items 1-7)

1. Concat reads the DAG manifest chunk records (`stages.tape_chunks.chunks`) in index order and loads each sidecar (`Path(output_path).with_suffix(".json")`). Validate BEFORE invoking ffmpeg:
   - indexes exactly `0..N-1` (no gaps, no duplicates, no reversed order);
   - core-range continuity: `chunk[i].core_end == chunk[i+1].core_start` for all i, first `core_start == 0`, last `core_end ==` source frame count;
   - codec-parameter consistency: all sidecars share identical `ffmpeg.path`, `ffmpeg.version`, `avg_frame_rate`, `cfr: true`, and they match `manifest["versions"]` (windows_ffmpeg_path, windows_ffmpeg_version, ffmpeg_cfr_policy) and the selected rate;
   - integrity: `sidecar["video"]["sha256"]` equals the sha256 of the actual chunk file (reuse `media_contract.sha256_of`).
   Reject (raise) before the ffmpeg call on any failure (TC-33: reversed order, missing, duplicate, parameter mismatch).
2. The concat runner generates its own concat list file under `work_root` (e.g. `work_root/output/concat_list.txt`):
   - resolve each chunk path (`Path.resolve()`); every resolved path MUST be under the trusted run root (resolved `work_root`; use `Path.is_relative_to`, available since 3.9);
   - reject BEFORE writing the list if any raw path string contains CR, LF, or a single quote;
   - never embed user-supplied strings into the list: build each line from the resolved `Path` object only. The concat demuxer requires single-quoted `file '...'` lines; that quoting is safe because the characters are rejected up front. Put a short comment explaining this.
3. Concat: run the pinned Windows FFmpeg (`manifest["versions"]["windows_ffmpeg_path"]`) with `-f concat -safe 0 -i <list> -c copy -an` -> `work_root/output/concat.mkv` (video-only FFV1 stream copy). Nonzero exit or probe failure -> fail closed with the stage error convention (category/message/command/exit_code/log_path/retryable, stderr tail).
4. Remux: same pinned FFmpeg: `-i concat.mkv -i <source path> -map 0:v:0 -map 1:a -c:v copy -c:a copy` -> `publish.final_mkv`. All source audio streams stream-copied (the source is guaranteed to have >=1 audio stream by preflight; still fail closed if the result has no audio stream).
5. Verify (final contract) on `publish.final_mkv`:
   - exact decoded video frame count (`media_contract.count_frames`) == source frame count;
   - video `avg_frame_rate` == the selected rate (compare as `Fraction`, helper `media_contract._rational`);
   - video stream: codec `ffv1`, pix_fmt `yuv422p10le`, progressive, selected fps;
   - width/height, `sample_aspect_ratio`/`display_aspect_ratio`, `color_range`/`color_space`/`color_primaries`/`color_transfer` all match the analyze/preprocessed manifest values;
   - audio: stream count == source audio stream count; per-stream codec == source codec; per-stream packet count == source packet count (`media_contract.audio_packet_counts`);
   - duration: `|video duration - container duration|` within a 0.5 s tolerance (document the constant);
   - input immutability (TC-36): `media_contract.verify_file_unchanged` for the source and the preprocessed artifact using the fingerprints stored in the manifest (find where analyze stores them);
   - JPEG is 0: no mjpeg/jpeg stream in the final mkv AND no `.jpg`/`.jpeg` files under `work_root/output`;
   - 8-bit boundary warning (TC-35): the verify report MUST include a warning that the TAPE 8-bit PNG/OpenCV boundary must not be represented at the same precision as the 10-bit FFV1 container, explicitly noting that the CLIP-feature FFmpeg `rgb24` decode and the reference pool PNGs are 8-bit.
6. After PASS: publish per design decision 3, write `work_root/output/verify_report.json`, and record the report on the verify stage record (metrics).
7. Stage-runner result contracts (the orchestrator already consumes these, keep them):
   - concat/remux runners return a dict with at least `output_path` (orchestrator uses `_result_path` + `wall_time_seconds`);
   - verify runner returns `{"contract_passed": bool, "report": {...}, "wall_time_seconds": ...}` (orchestrator at run_local.py:1833-1843 reads `contract_passed`).

## Tests (TDD: write the tests first, then implement)

Extend `tests/test_long_media_contract.py`. Follow the existing pattern in that file: real FFmpeg fixtures generated via `subprocess` (see `two_audio_fixture`, lines 37-89), probe-based assertions, `ROOT` sys.path setup, `pytestmark = pytest.mark.integration`.
Existing reusable fixtures: `two_audio_fixture` (320x240, 2 aac audio streams, 15 frames, rate 30000/1001, SAR 10/11), `two_audio_probe`, `interlaced_probe`.

New tests - each name must contain its TC id (tc33/tc34/tc35/tc36):
- TC-33a: 4 FFV1 chunks with identical codec parameters concatenate in frame order. Generate with real ffmpeg (e.g. one 100-frame source split into 4 contiguous 25-frame FFV1 `yuv422p10le` chunks with the trim filter + CFR, or 4 separate encodes). The concatenated result: decoded frame count == 100, rate preserved, duration within 100/fps tolerance.
- TC-33b: pre-concat rejection - reversed chunk order, missing chunk, duplicated chunk, parameter mismatch (different avg_frame_rate or ffmpeg version in one sidecar). Each must be rejected BEFORE any ffmpeg call: use a fake command runner / monkeypatched subprocess that asserts it was never invoked.
- TC-33c: safe-stem rule - a stem matching `^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$` is kept as-is; bad stems (leading dot, space, `/`, >80 chars) become `source-<hash12>`; the final output name always contains hash12.
- TC-33d: concat-list path safety - a chunk path outside the trusted run root is rejected; a raw path containing CR, LF, or a single quote is rejected before list creation.
- TC-34: remux of a video-only concat with the 2-audio fixture as source: the final mkv has exactly 2 audio streams, per-stream codec and packet count match the source; video comes from the concat; `|video duration - container duration| <= 0.5 s`. Use real ffmpeg for the remux in the test (generate a small video-only mkv first, then remux with two_audio_fixture).
- TC-34b: 0-audio source rejected by long preflight - the existing `test_long_preflight_rejects_source_without_audio` covers this; keep it green (no new test required).
- TC-35: final media contract - verify on a valid final mkv returns `contract_passed=True` and the report contains the 8-bit warning (assert the warning text mentions `rgb24` and the 8-bit reference pool PNGs); a corrupted final mkv (wrong pix_fmt, or a dropped color field, or frame-count mismatch) fails closed; JPEG count is 0.
- TC-36: non-destructiveness - before/after the verify run, source and preprocessed artifact size/mtime_ns/sha256 are unchanged (`verify_file_unchanged`); nothing is written under the input path; the existing `test_output_path_guard_rejects_same_or_descendant_path` stays green.
- Orchestration wiring: at least one test that drives `run_local.run`/`resume` with injected FAKE concat/remux/verify runners (same style as tests/test_run_local.py helpers - you may import run_local in this file) proving the executors wiring calls the injected runners and records stage state; and one test proving the DEFAULT (non-injected) verify path publishes to the monkeypatched `publish.public_dir`.

## Verification (run at the end; targeted only)

1. Windows fresh basetemp:
   `C:/Python313/python.exe -m pytest tests/test_long_media_contract.py tests/test_short_inference_contract.py -q --basetemp=E:/vhs-upscale/work/tmp/task12-win-<timestamp>`
2. Orchestrator regression (your run_local changes must not break it):
   `C:/Python313/python.exe -m pytest tests/test_run_local.py tests/test_manifest.py tests/test_tape_chunk.py -q --basetemp=E:/vhs-upscale/work/tmp/task12-win-reg-<timestamp>`
3. If WSL is reachable from this host, you MAY run the canonical gate:
   `wsl.exe -d Ubuntu-22.04 /bin/bash /mnt/e/vhs-upscale/work/logs/qtgmc-long-processing/task12_wsl.sh`
   (the coordinator re-runs it regardless).

## Conventions

- Reuse existing media_contract helpers: `ffprobe_json`, `count_frames`, `audio_packet_counts`, `_rational`, `sha256_of`, `capture_file_fingerprint`, `verify_file_unchanged`, `reject_output_path_overlap`. Do not duplicate them.
- run_local.py: `RunLocalError` for pipeline-level failures; atomic writes (temp + `os.replace`, cf. `references._write_json_atomic`); `_utc_timestamp`; error records `{category, message, command, exit_code, log_path, retryable}`.
- No new third-party dependencies. No GPU/WSL-GPU calls in tests (real ffmpeg subprocess on Windows is fine and already used by the existing fixtures).
- Tests hermetic: tmp_path/tmp_path_factory only; never write to `E:/vhs-upscale/work/tape` (monkeypatch `publish.public_dir`).
- Python 3.10 compatible (no 3.11+ syntax, no `StrEnum`, match statements OK if 3.10 - prefer plain code).

## Deliverables (report back)

- Changed files with a per-TC status table (TC-33/34/35/36: implementing function, covering test).
- Targeted verification output (the pytest summary lines from both Windows runs).
- Design notes + residual risks.
Do NOT commit. Do NOT run formatters/linters or the full test suite.
