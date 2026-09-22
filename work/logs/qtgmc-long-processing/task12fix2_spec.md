# Task 12 review-fix spec (reviewer findings F1-F5, commit a1ab594)

Worktree: `E:/vhs-upscale/worktrees/tape-qtgmc-long-processing` (branch `codex/tape-qtgmc-long-processing`).
Host Python: `C:/Python313/python.exe` (3.13). Code MUST stay Python 3.10 compatible.
Skip formatters, linters, project-wide suites. Do NOT commit.
Keep the working tree LF line endings (this repo is LF; do not write CRLF).

## Findings to fix (from Task12Review, VERDICT APPROVE_WITH_FIXES)

### F1 (Important) — no color-guessing fallbacks in the final contract
`pipeline/media_contract.py` `_final_video_field` (lines 217-226): `color_primaries` falls back to `color_space`, and `color_transfer` falls back to `color_trc` then `color_space`. Replace the guessing chain with fail-closed exact reads:
```python
def _final_video_field(stream, field):
    # ffprobe reports transfer characteristics under the "color_trc" key;
    # that key mapping is exact, not a fallback chain. No other guessing.
    if field == "color_transfer":
        value = stream.get("color_trc")
    else:
        value = stream.get(field)
    if value is None or str(value).strip() == "":
        raise PreflightError("final video lacks required metadata {!r}".format(field))
    return str(value)
```
Also in `pipeline/run_local.py` `_build_handoff_json` (~line 1704): `"color_transfer": video.get("color_transfer")` reads a key ffprobe never emits; change to `video.get("color_trc")` (keep the output key name `color_transfer` for schema stability).

### F4 (Minor) — width/height manifest check tautology
`pipeline/media_contract.py` `verify_final_media_contract` (lines 855-861): when `manifest["source"]["video"]` omits width/height, the expected value silently defaults to the final probe's own value (tautology). Replace with fail-closed: `source.get("video")` must be a Mapping with both `width` and `height`, else `raise PreflightError("manifest source.video width/height is missing")`; expected values = `int(source_video["width"])` / `int(source_video["height"])`.

### F5 (Minor) — dead constant
`pipeline/media_contract.py` line 68 `_SAFE_STEM_RE` is unused (naming actually uses `pipeline.paths.safe_source_stem`). Delete the constant; `re` then has no other use in the module — also remove `import re` (line 19).

### F2 (Important) — publish is not all-or-nothing
`pipeline/run_local.py` `_publish_verified_artifacts` (lines 1713-1751): five artifacts are copied sequentially to `public_dir`; the handoff json (the completion marker) is copied second, so a later copy failure leaves a partial `public_dir` that appears published. Rewrite:
1. Resolve `public_dir`, the five source paths (`final_mkv`, `run_manifest_snapshot`, `chunk_manifest_snapshot`, `reference_manifest_snapshot`, `handoff_json`), and build the handoff payload (probe is read-only).
2. Validate ALL five sources exist (`is_file()`) BEFORE writing anything; missing source -> `StageRunError` as today.
3. Copy the four non-handoff artifacts first, then the handoff json LAST, each via the existing atomic pattern (temp + `os.replace`).
4. Track the public destinations already written; on any `OSError`/`StageRunError` during copying, best-effort delete the already-written public destinations (ignore cleanup errors), then re-raise.
Net contract: the handoff json appears in `public_dir` only after all five artifacts are in place; on failure the stage record is FAILED and `public_dir` holds no partial publish (modulo best-effort cleanup failure, which the stage-FAILED record still marks).
Do NOT change the work_root/output-side handoff write (lines 1724-1726) — that is a work artifact. Do NOT change the publish block defaults (lines 1276-1285) or `_default_verify_runner` snapshot writes.

### F3 (Minor) — missing TC-35 dropped-color-field test
`tests/test_long_media_contract.py`: add a test (e.g. `test_tc35_final_media_contract_fails_on_missing_color_field`) mirroring the existing corrupted-pix_fmt test pattern (line ~731, READ it first): drive `verify_final_media_contract` (or the verify stage) against a valid final fixture whose probed stream lacks `color_primaries` (monkeypatch the probe the way that test does), and assert `PreflightError` (or the verify failure path) naming `color_primaries`. Also assert the `color_trc` mapping still works (the existing green TC-35 pass test already covers this — do not duplicate).

### F2 test
Add a publish all-or-nothing test (in `tests/test_long_media_contract.py`, near the TC-36 publish tests — READ the existing publish tests at lines ~962-994 first): arrange for the third copy (e.g. `run_manifest_snapshot`) to fail (e.g. point its publish source at a path that does not exist, or monkeypatch `shutil.copy2` to raise on that file) -> `run()`/`_default_verify_runner` raises StageRunError (or the stage is FAILED) and `public_dir` contains NO handoff json and NO partial artifacts (assert the directory is absent or empty). The existing success-path publish test must stay green.

## Verification (targeted only, fresh basetemps)

1. `C:/Python313/python.exe -m pytest tests/test_long_media_contract.py tests/test_run_local.py tests/test_manifest.py tests/test_chunks.py -q --basetemp=E:/vhs-upscale/work/tmp/task12fix2-win-<ts>`
2. Optionally WSL if reachable: task12_wsl.sh / task11_wsl.sh (coordinator reruns regardless).

## Deliverables
Changed files; changed/added test names; pytest summary lines; confirmation that LF endings were kept.
Do NOT commit. Do NOT run formatters/linters or the full suite.
