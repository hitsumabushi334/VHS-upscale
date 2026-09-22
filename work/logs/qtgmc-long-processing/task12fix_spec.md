# Task 12 fix spec: default-runner wiring (deviation from Task 12 spec design #4)

Worktree: `E:/vhs-upscale/worktrees/tape-qtgmc-long-processing` (branch `codex/tape-qtgmc-long-processing`).
Host Python: `C:/Python313/python.exe` (3.13). Code MUST stay Python 3.10 compatible.
Skip formatters, linters, project-wide suites. Do NOT commit.

## Background (why this fix)

The Task 12 implementation left:
- `_resolve_concat_runner` / `_resolve_remux_runner` (pipeline/run_local.py ~lines 1911-1926) returning `None` when no runner is injected (instead of the real default runner).
- The `_run_pipeline` `executors` dict (~lines 1999-2006) passing the raw injected values for `concat`/`remux`/`verify`, so CLI `run`/`resume`/`batch` leave those stages PENDING unless the caller injects runners.

The binding Task 12 spec (`E:/vhs-upscale/work/logs/qtgmc-long-processing/task12_spec.md`, design decisions #4 and #5) requires all three to resolve to their real default runners, exactly following the existing `_resolve_qtgmc_runner` pattern (run_local.py:1563), so that CLI `run`/`resume` execute real finalization (concat -> remux -> verify+publish) once the upstream stages are complete. Fix exactly this; change nothing else.

## Changes (write scope: pipeline/run_local.py, tests/test_run_local.py, tests/test_long_media_contract.py)

### 1. pipeline/run_local.py resolver bodies
- `_resolve_concat_runner`: `return _default_concat_runner` when `runner is None` (instead of `return None`).
- `_resolve_remux_runner`: `return _default_remux_runner` when `runner is None`.
- `_resolve_verify_runner`: unchanged (already returns `_default_verify_runner`).

### 2. pipeline/run_local.py executors dict (~lines 1999-2006)
Change only the three lines:
```python
        "concat": _resolve_concat_runner(manifest, concat_runner),
        "remux": _resolve_remux_runner(manifest, remux_runner),
        "verify": _resolve_verify_runner(manifest, verify_runner),
```
Do NOT touch the `qtgmc`, `references`, `tape_chunks` lines.

### 3. tests/test_run_local.py: replace `test_concat_remux_verify_left_pending_without_executors` (line 1075)

The old test pins the old contract (stages stay PENDING without injection) and must be replaced with a test proving the NEW contract. New test (name: `test_concat_remux_verify_resolve_default_runners_when_not_injected`):
- Build the fixture with the existing helpers `_prepare_orchestration(tmp_path, monkeypatch)` and `_orchestration_runners(work_root)` (lines 557/566).
- `run_local.canary(work_root, seconds=1, **runners)` first (stamps the canary signature).
- Monkeypatch the three module attributes the resolvers read at call time: `run_local._default_concat_runner`, `run_local._default_remux_runner`, `run_local._default_verify_runner` with recording fakes matching the orchestrator stage contracts:
  - concat fake: `def fake(manifest, **kwargs):` append `"concat"` to a local list; write `work_root / "output" / "concat.mkv"` (small bytes); return `{"output_path": str(path), "wall_time_seconds": 0.1}`.
  - remux fake: append `"remux"`; write `Path(manifest["publish"]["final_mkv"])` (small bytes); return `{"output_path": str(path), "wall_time_seconds": 0.1}`.
  - verify fake: append `"verify"`; return `{"contract_passed": True, "wall_time_seconds": 0.05}`.
  (No publish is expected: publish lives inside the real `_default_verify_runner`, which is replaced here.)
- `partial = {"qtgmc_runner": ..., "references_runner": ..., "chunk_runner": ...}` (only those three entries of `runners`).
- `report = run_local.run(work_root, **partial)`.
- Assert `report["stages"]["concat"]["status"] == "complete"`, same for `remux` and `verify`, and the recorded call list is `["concat", "remux", "verify"]`.

### 4. tests/test_long_media_contract.py: add `test_tc33_run_executes_default_finalization_end_to_end`

Full e2e proof that `run()` with NO concat/remux/verify injection executes the REAL default runners (real ffmpeg concat + remux + real final contract + real publish):
- Mirror the manifest/fixture construction of `test_tc33_default_verify_publishes_to_public_dir` (lines 962-994, which is proven green): `_base_manifest(tmp_path, frame_count=15, chunk_records=[...], source_path=two_audio_fixture, probe=interlaced_probe)`. READ that test first and reuse its exact wiring (it already proves `verify_final_media_contract` passes on `_make_ffv1_source` output with that manifest).
- Fake upstream runners (injected):
  - qtgmc: write `work_root / "qtgmc" / "preprocessed.mkv"` and return `{"output_path": ..., "frame_count": 15, "fps": "30000/1001"}` — the manifest's `preprocessed` fingerprint must match the written file (check how `_build_dag_manifest` / the injected-runner test at lines 821-959 handle it and replicate; `verify_final_media_contract` checks `verify_file_unchanged` for both source and preprocessed).
  - references: write `work_root / "reference-cache" / "cache" / "manifest.json"` and return `{"reference_cache_key": "cache", "reference_root": str(ref_root), "reference_manifest_hash": ...}` (mirror line 914-922 of the injected test).
  - chunk: produce a REAL FFV1 chunk via the existing `_make_ffv1_source(chunk_mkv, frames=15)` at production layout `work_root / "tape" / "chunks" / "000000" / "chunk.mkv"`, then write a valid sidecar `chunk.json` next to it with the full tape_chunk sidecar contract (schema_version 1, chunk_index 0, core_start 0, core_end 15, decode_start 0, decode_end 15, frame_hashes [...], reference_cache_key, checkpoint_sha256, ffmpeg `{"path": <manifest versions.windows_ffmpeg_path>, "version": <manifest versions.windows_ffmpeg_version>}`, cfr True, avg_frame_rate "30000/1001", metrics {}, stderr_log "wsl.log", video `{"path": "chunk.mkv", "size": <bytes>, "sha256": <sha256 of the actual file>}`). The chunk record in the manifest: `{"index": 0, "status": "complete", "output_path": str(chunk_mkv), "hash": <sha256>, "mtime_ns": <mtime>}`.
- Monkeypatch `publish.public_dir` in the manifest to `tmp_path / "public"` (tests never write to the real `E:/vhs-upscale/work/tape`).
- Stamp the canary signature: `manifest["canary"] = {"signature": run_local._canary_signature(manifest), "seconds": 1}` and save (required for `mode="run"`).
- Call `run_local._run_pipeline(work_root, mode="run", qtgmc_runner=..., references_runner=..., chunk_runner=...)` — NO concat/remux/verify args.
- Assert: all six stages `complete`; the public dir contains the 5 published files (final mkv, handoff json, run/chunk/reference manifest snapshots — names per `mc.public_artifact_basename` and the manifest's publish snapshot field names); the verify stage record has `contract_passed: True` and `metrics.report` contains the 8-bit boundary warning.
- The real default remux runs `-i concat.mkv -i <two_audio_fixture> -map 0:v:0 -map 1:a -c:v copy -c:a copy`; both inputs are real files. If any field mismatch appears (e.g. width 352x240 of `_make_ffv1_source` vs manifest video), mirror whatever `_base_manifest`/the TC-35 tests already reconcile — do not weaken the verify contract.

## Verification (targeted only, fresh basetemps)

1. `C:/Python313/python.exe -m pytest tests/test_run_local.py tests/test_long_media_contract.py tests/test_short_inference_contract.py -q --basetemp=E:/vhs-upscale/work/tmp/task12fix-win-<timestamp>`
2. Optionally, if WSL reachable: `wsl.exe -d Ubuntu-22.04 /bin/bash /mnt/e/vhs-upscale/work/logs/qtgmc-long-processing/task12_wsl.sh` (the coordinator re-runs it regardless).

## Deliverables

- Changed files; new/changed test names; pytest summary lines from verification run 1; any fixture adjustments made and why.
Do NOT commit. Do NOT run formatters/linters or the full test suite.
