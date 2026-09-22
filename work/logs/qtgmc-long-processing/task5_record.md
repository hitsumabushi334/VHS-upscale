### Task 5 implementation record (2026-09-21)

- commit: `5cf78be` `refactor: share fail-closed media contracts`
- files: `pipeline/media_contract.py` (new, +548), `scripts/run_short_inference.py` (net -109, pure extract-and-import), `tests/test_long_media_contract.py` (new, +314)
- scope: preflight/analyze fail-closed contracts + shared primitives (ffprobe_json, count_frames, audio_packet_counts, _rational, field-order mapping, override JSON schema with per-field provenance, input fingerprint/verify, output-path guard). No audio-stream policy in the shared module; short runner keeps exactly-1-audio, long preflight accepts >=1 (0 rejected pre-run) and records index/codec/packet count per audio stream.
- PLAN DEVIATION (recorded): Task 5 = preflight/analyze + shared primitives only; concat/remux/FFV1 verification/publish deferred to Task 12 (plan L844 "Extend: pipeline/media_contract.py").
- fresh verification (coordinator, 2026-09-21):
  - rsi.* symbol sweep: all 14 required symbols present in run_short_inference.py (no re-export needed).
  - WSL harness regression: `52 passed in 43.16s` (long 27 + short 25), EXIT_CODE=0 (log: work/logs/qtgmc-long-processing/regression-wsl-20260920.log).
  - Both handoff blockers resolved as pre-existing environment issues, not Task 5 regressions: (A) Windows pytest basetemp WinError 5 scandir (0o700 ACL; non-elevated, undeletable) - WSL harness works; (B) Windows FFmpeg 8.1.1 drops color_primaries/color_transfer from the short fixture - WSL FFmpeg writes them correctly.
- post-review fix commit: `4a5f4ec` `fix: pin field-order provenance and unify missing-value set` (see review record).

### Task 5 review record (2026-09-21)

- reviewer: cursor/glm-5.2 (high, read-only)
- first pass (target: 5cf78be, parent f1f0100): APPROVE_WITH_CONDITIONS - Critical 0 / Important 0 / Minor 4, behavior preservation PASS, fail-closed PASS, no scope violation.
  1. tt/bb + matching-override provenance semantics unpinned - pinned with comment (override always wins provenance) + parametrized cases (tt,tff) and (bb,bff).
  2. _resolve_media_field missing-value literal set inconsistent with _MISSING_VALUES ("N/A") - unified to _MISSING_VALUES + new test (uppercase "N/A" rejected).
  3. output-path guard asymmetric - comment: symmetric case (input under output) intentionally deferred to Task 12 publish stage.
  4. ffprobe field_order enum assumption - comment: ffprobe 8.1.1 emits exactly tt/bb/progressive; other spellings are ambiguous-by-design.
  5. scope note: trailing alias block commented as Task 12 forward-compatibility hooks.
- fix commit: `4a5f4ec` (+15/-4 across 2 files; no behavior change).
- re-review (target: f1f0100..4a5f4ec): APPROVE - Critical 0 / Important 0 / Minor 0; all 4 prior items RESOLVED with file:line evidence; behavior preservation PASS; fail-closed PASS; no scope deviations.
- fresh verification (coordinator, WSL): `55 passed in 41.50s` (long 30 + short 25), EXIT_CODE=0 (log: work/logs/qtgmc-long-processing/regression-wsl-fix-20260921.log).
- note: the re-reviewer's own test run hit a transient system-level WSL E_ACCESSDENIED and fell back to Windows Python; long suite 30/30 passed there and the short-suite failures were the known Windows FFmpeg fixture metadata gap (environmental, not a regression). Coordinator WSL run is the canonical 55/55 evidence.
