# Task 13 follow-up fix spec (batch path disk-gate gap, post-86bd38f)

Worktree: `E:/vhs-upscale/worktrees/tape-qtgmc-long-processing` (branch `codex/tape-qtgmc-long-processing`).
Host Python: `C:/Python313/python.exe` (3.13). Code MUST stay Python 3.10 compatible.
Skip formatters, linters, project-wide suites. Do NOT commit.

## Context

The re-review of 4cfa7c0..86bd38f (APPROVE, 0/0/0) left one residual: `batch()` calls
`manifest = _ensure_dag_manifest(work_root)` (which PERSISTS the DAG manifest when absent)
before invoking `canary`/`run`. Because `analyze`'s own gate uses the weaker threshold
`ceil(ffv1_required * 1.20)` (no PNG term, measured at `root.parent`) while the run-path
gate uses `ceil((ffv1_required + png_worst_case) * 1.20)` (measured at the work root), there
is a deterministic window: `analyze_threshold <= free < run_threshold` where `analyze`
passes (writing its own preview artifacts — allowed), then `batch` writes the DAG manifest,
then the run-path gate raises `InsufficientDiskError` with a DAG artifact already on disk.
The operator guide (docs/qtgmc-long-processing.md, disk policy section) states the pipeline
"does not create any artifacts" on gate failure — make that true for the batch path too.

## Change (write scope: pipeline/run_local.py, tests/test_run_local.py)

In `batch()` only — do not touch `_run_pipeline`, `_enforce_disk_gate`, or `analyze`:

1. Replace the pre-run `manifest = _ensure_dag_manifest(work_root)` with a READ-ONLY load:
   - `dag_path = _dag_manifest_path(work_root)` (run_id=None derives the run_id from the preview manifest — no file write), then
     `manifest = _read_json(dag_path) if dag_path.is_file() else None`.
2. Canary decision: if the loaded manifest exists AND has a `canary` record, skip canary;
   otherwise call `canary(work_root, seconds=1, **pipeline_kwargs)` (the canary goes through
   `_run_pipeline`, whose disk gate now runs before the DAG bootstrap).
3. Call `run(work_root, **pipeline_kwargs)` as before; on success, load the DAG manifest
   (now guaranteed to exist; `_ensure_dag_manifest` or a plain JSON read) and fill
   `entry["run_id"]`, `entry["manifest_path"]`, `entry["reference_cache_root"]` from it,
   then `entry["status"] = report["status"]`.
4. Failure handling unchanged: per-source `entry["status"] = "failed"`, `entry["error"]`.
   On failure those three manifest fields are simply absent from the entry.

## Test (tests/test_run_local.py)

`test_tc37_batch_disk_gate_creates_no_dag_on_first_batch`:
- Fresh orchestration-style fixture: analyze at a free value ABOVE the analyze threshold
  (so analyze writes its preview artifacts), selection present, no DAG manifest yet.
- Inject `disk_usage_fn` returning a free value in the window
  `[ceil(ffv1*1.20), ceil((ffv1+png)*1.20))` (compute both thresholds from the estimate
  report fields exactly like `test_tc37_run_stops_before_artifacts_when_disk_low` does).
- Call `run_local.batch(...)` (one source).
- Assert: the run entry has `status == "failed"` and an `InsufficientDiskError` message in
  `error`; the DAG manifest path does NOT exist; the qtgmc output path does NOT exist;
  no runner was called (reuse the existing fake-runner call counter pattern).

## Verification (targeted only, fresh basetemps)

`C:/Python313/python.exe -m pytest tests/test_run_local.py tests/test_manifest.py tests/test_chunks.py -q --basetemp=E:/vhs-upscale/work/tmp/task13fix2-win-<ts>` — all must pass (currently 96 + 1 new = 97).

## Deliverables

- Changed files; new test name; pytest summary line; confirmation that batch performs no
  manifest-persisting write before the run-path disk gate.
Do NOT commit. Do NOT run formatters/linters or the full suite.
