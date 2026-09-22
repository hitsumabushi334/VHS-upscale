# Task 13 review-fix spec (reviewer findings, commit 4cfa7c0)

Worktree: `E:/vhs-upscale/worktrees/tape-qtgmc-long-processing` (branch `codex/tape-qtgmc-long-processing`).
Host Python: `C:/Python313/python.exe` (3.13). Code MUST stay Python 3.10 compatible.
Skip formatters, linters, project-wide suites. Do NOT commit.

## Findings (from the independent reviewer)

### F1 (Important): disk gate runs after DAG manifest bootstrap
`_run_pipeline` (pipeline/run_local.py ~L2370) calls `manifest = _ensure_dag_manifest(root)` BEFORE `_enforce_disk_gate(...)` (~L2391). `_ensure_dag_manifest` persists the DAG manifest when absent, so a first `run`/`resume`/`canary`/`batch` entry on low disk can create `manifest.json` before the gate raises `InsufficientDiskError`. Spec requires failing before creating artifacts.

Fix:
1. In `_run_pipeline`, move the disk gate BEFORE `_ensure_dag_manifest(root)`.
2. Derive the gate inputs without bootstrapping: load the preview manifest via the existing `_load_manifest(root)` for `source.frame_count` and width/height (keep the existing video-dict/source-level fallback logic, and the preview-manifest fallback that already exists at ~L2387).
3. For the measured FFV1 term, read the DAG manifest ONLY if `_dag_manifest_path(root)` already exists (load its JSON); otherwise pass `None` (conservative formula). Refactor `_enforce_disk_gate` to accept the already-loaded DAG manifest dict (or `None`) instead of relying on a bootstrap side effect; keep the shared formula helpers unchanged.
4. After the gate passes, `_ensure_dag_manifest(root)` runs exactly as before (the rest of the function is unchanged).
5. Update the operator guide line in docs/qtgmc-long-processing.md that says the gate fails before creating "new stage artifacts" to say "any artifacts" (now true for the run path too).

### F2 (Minor): no test locks the disk-gate margin to the estimate formulas
None of the TC-37 tests assert the gate threshold equals `ceil((ffv1_required + png_worst_case) * 1.20)` or compares the gate's required bytes to the estimate report fields. Add assertions (extend `test_tc37_run_stops_before_artifacts_when_disk_low` or add a focused test): the injected free bytes are below the threshold computed from the estimate report's `ffv1_free_space_margin.required_bytes` + `png_worst_case` value (recompute `ceil(... * 1.20)`), and a free value of exactly that threshold does NOT raise (boundary check if tractable with the injected disk_usage_fn — the gate rejects when free < safe_required).

### F3 (Minor): weak post-condition in test_tc37_run_stops_before_artifacts_when_disk_low
The qtgmc output path check allows a pre-canary placeholder (`not exists or read_bytes() == b"qtgmc-output"`). Tighten: assert the path does not exist, UNLESS the fixture legitimately pre-creates the placeholder — if it does, assert instead that the DAG manifest (if present) records no COMPLETE stage and no stage artifacts exist. Keep the `not calls` assertion.

### F4 (Minor): test_tc37_estimate_cli_prints_json is thin
Extend: stdout JSON must also contain `predicted_tape_time` (dict) and `eta_seconds` keys, and `input_frame_count` must equal the fixture frame count. Keep it a single run.

### F5 (Minor): measured test omits predicted_tape_time label
In `test_tc37_estimate_measured_with_completed_chunks`, also assert `report["predicted_tape_time"]["label"] == "measured"` (benchmark resolution fixture).

## Verification (targeted only, fresh basetemps)

`C:/Python313/python.exe -m pytest tests/test_run_local.py tests/test_manifest.py tests/test_chunks.py -q --basetemp=E:/vhs-upscale/work/tmp/task13fix-win-<ts>` — all must pass (currently 96).

## Deliverables

- Changed files; test names added/strengthened; pytest summary line; confirmation the gate now precedes `_ensure_dag_manifest`; the exact boundary semantics implemented for F2.
Do NOT commit. Do NOT run formatters/linters or the full suite.
