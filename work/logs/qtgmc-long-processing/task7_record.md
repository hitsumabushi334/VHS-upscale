### Task 7 implementation record (2026-09-21)

- commits: bb912cc (feat: add QTGMC preview selection gate), 6e85726 (fix: remove redundant alias fields and dead ternary in run_local), 894fcd3 (fix: remove remaining qtgmc_wall_time alias in failed candidate path)
- files: pipeline/run_local.py (+1053, then -14 net over both fixes), tests/test_run_local.py (+417)
- scope per plan Task 7 + TC-37: analyze/preview/select/status CLI in run_local; preview renders a 6-candidate matrix (3 presets x 2 tape input rates) over at least 3 source ranges; candidate keys preset-60000_1001 / preset-30000_1001; each candidate carries source timestamp, frame range, frame_count, fps, wall-time seconds, tape_fps, peak_vram_mb (null when telemetry missing); a failed candidate keeps all sibling successes (matrix_status incomplete, successes preserved); select() writes selection.json (preview_matrix_sha256, preset, tape_input_rate, selected_at, optional note - no automatic metric scoring) and rejects unknown/failed candidates and hash mismatch; validate_selection() gate for downstream publish; TC-37 first-half estimate: estimate reports predicted tape fps only when resolution is the 704x396 benchmark and labels values estimate, never measured, when unmeasured; disk-space gate runs before any artifact is created; qtgmc/tape runners are injectable and lazily imported (no top-level torch, vapoursynth, or pipeline.manifest imports).
- PLAN DEVIATION: none.

### Task 7 review record (2026-09-21)

- reviewer: cursor/glm-5.2 (high, read-only)
- first pass (target: bb912cc): APPROVE_WITH_CONDITIONS - Critical 0 / Important 1 / Minor (incl. accepted-as-is MINOR-4).
  1. (IMPORTANT-2) dead ternary in build_estimate_report plus redundant duplicate alias WRITE keys (input_sha256, output_sha256, qtgmc_wall_time, tape_wall_time, peak_vram) in the initial and success-path candidate updates - fixed in 6e85726.
  2. (MINOR, accepted as-is) kept per reviewer disposition.
- post-review fix (6e85726): removed the dead ternary and all duplicate alias WRITE keys from both candidate dict updates.
- re-review found one remaining duplicate qtgmc_wall_time WRITE key in the failed-path candidate.update; coordinator removed it directly in 894fcd3 (single-line deletion).
- final re-review (scope bb912cc..894fcd3): APPROVE - Critical 0 / Important 0 / Minor 0 within scope (one pre-existing, out-of-scope MINOR noted: redundant if/else in build_estimate_report predating bb912cc, kept).
  - PREVIOUS_FINDINGS_RESOLUTION: IMPORTANT-2 RESOLVED by 6e85726 (success-path candidate.update now contains exactly 10 unique keys, each written once); remaining failed-path duplicate RESOLVED by 894fcd3 (failure path now status + error + exactly one wall-time write); worktree-wide search confirms no remaining consumers of the removed alias keys.
  - BEHAVIOR_PRESERVATION: READ-side fallbacks unchanged (_wall_time wall_time_seconds/wall_time; _fallback_hash input_hash/input_sha256/output_hash/output_sha256 and output_hash/output_sha256/sha256; peak_vram_mb/peak_vram); removed keys were pure WRITE-side aliases, so manifest values are unchanged; validate_selection requires preview_matrix_sha256/preset/tape_input_rate/selected_at only.
  - FAIL_CLOSED: runner exception still yields status failed + error dict + single wall-time measurement; select() and validate_selection() reject any non-complete candidate; matrix_incomplete still set on any exception; no fail-open path introduced.
  - TEST_EVIDENCE: WSL gate 21 passed, 1 deselected, EXIT_CODE=0 (gate7fix2-wsl-20260921.log); Windows regression 64 passed (test_short_inference_contract, test_long_media_contract, test_chunks, test_manifest; vapoursynth-requiring suites expected-red on this host); Windows collect-only: test_run_local 10, test_qtgmc_contract 12, test_short_inference_contract 25, test_long_media_contract 30, test_manifest 34, test_chunks 30, test_gpu_acceptance 3 (torch-importing suites collect-error on Windows as expected). git diff bb912cc..894fcd3 -- tests/test_run_local.py is empty, so no test was weakened by the fixes.
  - SCOPE_DEVIATIONS: none. Commits touch only pipeline/run_local.py; no top-level torch/vapoursynth/pipeline.manifest imports introduced.

