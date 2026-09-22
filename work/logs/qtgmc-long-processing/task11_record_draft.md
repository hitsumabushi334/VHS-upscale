# Task 11 record draft (apply after re-review verdict)

### Task 11 implementation record (2026-09-21)

- commits: c75c0f1 (feat: orchestrate resumable local TAPE runs), b43616d (fix: remove dead orchestrator duplicates, header imports, self-contained liveness check), 140bfdf (fix: use Python 3.10-compatible Enum for StageStatus), 318341a (fix: harden Task 11 fail-closed behavior per review findings)
- files: pipeline/run_local.py, tests/test_run_local.py (additive; original 10 tests byte-identical vs d3ae30f, verified by AST body comparison), pipeline/manifest.py (2-line Enum compat fix)
- scope per plan Task 11 + TC-17/TC-18/TC-19/TC-20/TC-21/TC-26/TC-32/TC-39:
  - DAG RunManifest at work_root/<run_id>/manifest.json via pipeline.manifest (preview manifest work_root/manifest.json unchanged); per-stage signatures; decide_stage skip only on full signature+artifact+contract match (TC-17, decision=skipped_valid_cache).
  - Corruption detection: hash mismatch quarantines the artifact to .invalid-<timestamp> and re-runs the stage and downstream only (TC-18); invalidated tape_chunks resets all per-chunk statuses to pending so chunks actually re-execute (review fix F1).
  - Selection/config change invalidation per the TC-19 dependency table, applied on load via _reconcile_manifest_changes for both run and resume (review fix F4).
  - Source change invalidates all stages; mtime-only change warns but preserves hash-based identity (TC-20).
  - resume re-runs only failed/interrupted/invalid stages and only resumable chunks; complete chunk mtime/hash unchanged (TC-21).
  - hidden --fault-inject-chunk (argparse.SUPPRESS) on run/canary/resume; parser-level env gate (PIPELINE_ENABLE_FAULT_INJECTION=1) plus pipeline-level RunLocalError gate; fault_injected=true recorded on the chunk (TC-21).
  - KeyboardInterrupt marks the current stage interrupted, persists, re-raises.
  - GPU lock work_root/.tape-gpu.lock: PID/host/run id/started_at; foreign host never replaced; live PID never stolen; stale lock replaced only after explicit recovery determination, via unlink + O_EXCL create with bounded retry (TC-32; review fix F7); lock serializes references + per-chunk tape_chunks (GPU stages); release only when the lock payload matches own pid+started_at.
  - canary: short QTGMC-only window, signature stamped only when qtgmc reaches complete with contract_passed (fail-closed, review fix F2); run() requires a matching canary signature, resume() does not (TC-21).
  - Windows liveness check fail-closed: any OpenProcess/GetExitCodeProcess failure is treated as live; dead only on a successful query reporting exit code != STILL_ACTIVE (259) (review fix F3).
  - batch: per-source failure isolation, namespaced work roots source-<sha12>, shared GPU lock root, summary lists each run's status and manifest path (TC-39).
  - unwired (non-injected) stages stay pending; downstream stages stay pending; report lists per-stage status and decision.
- PLAN DEVIATION: (1) the default GPU-lock liveness check does not use psutil (not declared in requirements) and does not compare started_at; any live PID is treated as live and unproven liveness refuses to steal (fail-closed). (2) StageStatus uses (str, Enum) instead of StrEnum because the canonical WSL venv is Python 3.10 (StrEnum is 3.11+).
- coordinator verification (2026-09-21): Windows targeted (C:/Python313, fresh basetemp): tests/test_run_local.py + tests/test_manifest.py -> 61 passed. Canonical WSL gate (wsl.exe -d Ubuntu-22.04, venv python 3.10, task11-wsl-fix-20260921.log): tests/test_run_local.py + tests/test_manifest.py + tests/test_chunks.py -> 91 passed, EXIT_CODE=0. Pre-existing Windows-only full-suite failures (test_short_inference_contract.py / test_qtgmc_contract.py, 8F+10E, ffprobe fixture color metadata) confirmed identical at d3ae30f and green on WSL (task10-wsl-20260921-6.log: 62 passed).

### Task 11 review record (2026-09-21)

- reviewer: independent reviewer subagents (read-only; harness equivalent of the established cursor/glm-5.2 high gate).
- initial review (scope d3ae30f..140bfdf): REJECT. Critical 1 (invalidated tape_chunks kept per-chunk complete -> no re-execution; TC-18/TC-19 gap), Important 3 (canary signature stamped after pending/failed QTGMC -> run() gate not fail-closed; Windows liveness fail-open on OpenProcess/GetExitCodeProcess failure; _ensure_dag_manifest overwrote selection without invalidation -> resume with changed selection kept stale artifacts), Minor 3 (--fault-inject-chunk missing on canary/resume CLI + env gate not a parser error; weak TC-18/19/21/32/39 coverage; stale lock replace via os.replace race).
- fix commit 318341a addressed all 7 findings (F1-F7) with TDD: strengthened TC-18/TC-19/TC-21/TC-32/TC-39 tests plus a new canary fail-closed test; original 10 tests byte-identical.
- re-review verdict: [PENDING]
- TEST_EVIDENCE (coordinator): Windows 61 passed (test_run_local + test_manifest); canonical WSL gate 91 passed, EXIT_CODE=0 (task11-wsl-fix-20260921.log).
