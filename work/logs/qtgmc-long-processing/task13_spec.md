# Task 13 spec: long-run estimates, ETA, disk gate, operator guide

Worktree: `E:/vhs-upscale/worktrees/tape-qtgmc-long-processing` (branch `codex/tape-qtgmc-long-processing`).
Host Python: `C:/Python313/python.exe` (3.13). Code MUST stay Python 3.10 compatible.
Skip formatters, linters, project-wide suites. Do NOT commit.

## Background / existing scaffolding (reuse, do not reinvent)

- `build_estimate_report(...)` (pipeline/run_local.py:313-402): pure TC-37 report builder. Existing fields: `input_frame_count`, `selected_fps`, `resolution`, `tape_fps {value,label,benchmark_resolution}` ("measured" only when value present AND resolution == `BENCHMARK_RESOLUTION`), `qtgmc_measured {value,label}`, `png_worst_case {value,label}`, `ffv1_free_space_margin {value,label,free_bytes,required_bytes}`, `predicted_tape_time` object + flat aliases (`predicted_tape_time_seconds`, `qtgmc_wall_time_seconds`, `png_worst_case_bytes`, `ffv1_free_space_margin_bytes`, `tape_wall_time_seconds`).
- `BENCHMARK_RESOLUTION = (704, 396)` (line 46), `InsufficientDiskError` (line 65).
- Disk gate ALREADY exists in `analyze` (injectable `disk_usage_fn`; raises `InsufficientDiskError` before any artifact; test `test_insufficient_disk_stops_before_creating_artifacts` at tests/test_run_local.py:373; test `test_estimate_report_never_calls_other_resolution_tape_fps_measured` at :324). Keep both green.
- `status()` (line 928) already returns `manifest.get("estimate")` — the estimate block belongs in the preview manifest.
- CLI has subcommands analyze/preview/select/status/canary/run/resume/verify/batch (parser ~line 2507). NO `estimate` subcommand yet.
- The tape_chunks orchestrator stage does NOT record per-chunk `wall_time_seconds` in chunk records (record update ~line 2160: status/output_path/hash/mtime_ns only).

## Changes (write scope: pipeline/run_local.py, tests/test_run_local.py, docs/qtgmc-long-processing.md)

### 1. pipeline/run_local.py — per-chunk wall time
In the tape_chunks stage branch (chunk_record.update, ~line 2160-2166), add `"wall_time_seconds": result.get("wall_time_seconds")` to the chunk record. Nothing else in that branch.

### 2. pipeline/run_local.py — `estimate(work_root, disk_usage_fn=None)`
New public function (add to `__all__` if the module keeps one):
- Load the preview manifest (required; RunLocalError if missing). Load selection JSON (via existing `validate_selection` or `_selection_path`+json) for the selected preset and `tape_input_rate`; load the DAG manifest (`_dag_manifest_path`) if present.
- Inputs for `build_estimate_report`:
  - `input_frame_count` = `manifest["source"]["frame_count"]`
  - `selected_fps` = selection's `tape_input_rate` (str), else `None`
  - `resolution` = (source width, source height)
  - `tape_fps` = measured from completed chunks: `sum(core frame counts of completed chunks) / sum(their wall_time_seconds)`; `None` if no completed chunk has a usable wall_time_seconds. Core frame count = `core_end - core_start` from the chunk record (or sidecar if the record lacks it; records should have output_path/hash only, so read the sidecar JSON next to `output_path` for core_start/core_end — use the existing sidecar loader if one exists).
  - `qtgmc_wall_time_seconds` = qtgmc stage record `metrics.wall_time_seconds` if the stage is COMPLETE, else `None`
  - `png_worst_case_bytes` = `frame_count * (width * height * 3 + width + 64)` (documented conservative RGB24 PNG upper bound: raw + per-scanline filter byte + chunk overhead)
  - `ffv1_required_bytes` = if completed chunks exist: `total measured chunk file bytes / total covered core frames * frame_count` (measured extrapolation); else `frame_count * width * height * 3 // 4` (conservative 2-bit/pixel 10-bit upper bound). Document the formula in the docstring.
  - `free_bytes` = `disk_usage_fn(work_root).free` with `disk_usage_fn` defaulting to `shutil.disk_usage` (injectable for tests, mirroring `analyze`)
- Wrap the `build_estimate_report` result and ADD these top-level fields (do not remove existing ones):
  - `"source_duration_seconds"`: source duration from the manifest probe if present, else `None`
  - `"selected_preset"`: selection preset name, else `None`
  - `"qtgmc_fps"`: `{"value": frame_count / qtgmc_wall_time if available else None, "label": "measured" if available else "estimate"}`
  - `"chunk_counts"`: `{"completed": N, "failed": N, "pending": N}` from the tape_chunks stage records (`pending` = expected total chunks - completed - failed; expected total from the chunk plan/ranges in the DAG manifest — derive the way the stage derives its loop; `None` for pending if the DAG manifest is absent)
  - `"eta_seconds"`: `mean(wall_time_seconds of completed chunks) * pending_chunks`; `None` if no completed chunk
  - `"measured_tape_resolution"`: the source resolution list (documents at what resolution tape_fps was measured)
  - `"feature_cache_bytes"`: measured total file size under the reference cache root if it exists, else `None`
  - `"log_bytes"`: measured total size of `*.log` files under work_root (top level), else `0`
- `predicted_tape_time` object: add a `"label"` key — `"measured"` when tape_fps is measured, `"rough_extrapolation"` when a value exists but resolution != benchmark, `"estimate"` when value is None. (Do not break existing assertions; they check `value` only.)
- Persistence: write the full report into the preview manifest under `"estimate"` (atomic write via the existing atomic manifest writer; update `updated_at`), and return the report dict.
- `status()` needs no change (already surfaces `manifest.get("estimate")`).

### 3. pipeline/run_local.py — disk gate in `_run_pipeline`
At the start of `_run_pipeline` (after mode/canary validation, before ANY stage runs): compute `required = ffv1_required + png_worst_case` using the same deterministic formulas as `estimate` (factor them into small private helpers so estimate and the gate share them); `free = disk_usage_fn(root).free`. If `free < required * 1.2` (20% safety margin) raise `InsufficientDiskError`. Accept `disk_usage_fn=None` parameter threaded through `run`/`resume`/`canary`/`batch` signatures (default `shutil.disk_usage`) so tests can inject. The existing `analyze` gate stays unchanged.

### 4. pipeline/run_local.py — CLI `estimate` subcommand
Add `estimate_parser = subparsers.add_parser("estimate")` with `--work-root` (same pattern as `status`); dispatch to `estimate()`; print the report as JSON (same output style as `status`); return 0; on RunLocalError/InsufficientDiskError print a JSON error report and return 1 (mirror `analyze`'s error handling).

### 5. docs/qtgmc-long-processing.md — operator guide
Add a new top-level section (after the Task records, before or after as the file structure dictates — keep numbering consistent): `## Operator guide: long-run estimates` (English) covering:
- `python real_world_inference.py estimate --work-root <work_root>` and `status`;
- report field meanings: `tape_fps.label` = `measured` only at the 704x396 benchmark resolution with completed chunk timings; `rough_extrapolation` for other resolutions; `estimate`/None when unmeasured — never present an unmeasured number as measured;
- `eta_seconds` = moving average over completed chunks (arithmetic mean) x pending chunks; absent until at least one chunk completes;
- disk policy: required bytes = FFV1 estimate + PNG worst case (+20% safety margin); the pipeline fails closed with `InsufficientDiskError` before creating artifacts when free space is below that;
- what to watch during a long run (status/estimate output, chunk counts, ETA).

## Tests (tests/test_run_local.py, TC-37)

Existing tests that must stay green: `test_estimate_report_never_calls_other_resolution_tape_fps_measured` (:324), `test_insufficient_disk_stops_before_creating_artifacts` (:373), and the existing `status` test (:416).

New tests (use the existing helpers `_analyze`, `_runners`, `_prepare_orchestration`, `_orchestration_runners`, `_disk` where applicable — READ tests/test_run_local.py:340-440 first for patterns):
1. `test_tc37_estimate_unmeasured_without_chunks`: analyze+preview+select fixture, no run → `estimate()`: tape_fps value None + label != "measured", `predicted_tape_time_seconds` None, `eta_seconds` None, chunk_counts all 0 (or pending None), free_bytes int, `feature_cache_bytes` None, report persisted (`status()["estimate"]` is not None).
2. `test_tc37_estimate_measured_with_completed_chunks`: orchestration fixture; inject fake chunk runner completing 2 chunks at 704x396 whose records carry `wall_time_seconds` (drive via `run_local.run` with fakes, then `estimate()`) → tape_fps label "measured", value == total_frames/total_wall, predicted time == frames/fps, eta_seconds == mean_wall * pending, chunk_counts correct.
3. `test_tc37_estimate_other_resolution_rough_extrapolation`: source resolution 1280x720 (probe override) + completed chunks with wall times → `tape_fps.label != "measured"` AND `predicted_tape_time["label"] == "rough_extrapolation"`.
4. `test_tc37_run_stops_before_artifacts_when_disk_low`: orchestration fixture; monkeypatch/inject `disk_usage_fn` returning tiny free (reuse the `_disk` helper) → `run_local.run(...)` raises `InsufficientDiskError` and no runner was called and no stage artifacts exist (mirror test :373 assertions).
5. `test_tc37_estimate_cli_prints_json`: `run_local.main(["estimate", "--work-root", ...])` returns 0 and stdout parses as JSON with `input_frame_count`.
6. Chunk record wall time: covered implicitly by test 2 (estimate reads it from records) — assert the chunk record in the DAG manifest contains `wall_time_seconds` in test 2.

## Verification (targeted only, fresh basetemps)

1. `C:/Python313/python.exe -m pytest tests/test_run_local.py tests/test_manifest.py tests/test_chunks.py -q --basetemp=E:/vhs-upscale/work/tmp/task13-win-<ts>`
2. Optionally, if WSL reachable: `wsl.exe -d Ubuntu-22.04 /bin/bash /mnt/e/vhs-upscale/work/logs/qtgmc-long-processing/task11_wsl.sh` (coordinator reruns it regardless).

## Deliverables

- Changed files; new function/test names; pytest summary lines; the exact disk-gate margin rule and formulas as implemented; doc section heading added.
Do NOT commit. Do NOT run formatters/linters or the full test suite.
