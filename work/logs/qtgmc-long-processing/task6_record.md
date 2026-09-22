### Task 6 implementation record (2026-09-21)

- commits: `a16a2cd` `feat: add atomic QTGMC FFV1 rendering` + `bac252e` `fix: drain vspipe stderr concurrently and clean stale mux partials`
- files: `vs_scripts/__init__.py`, `vs_scripts/qtgmc.py` (+885), `vs_scripts/render_qtgmc.vpy` (+76), `tests/test_qtgmc_contract.py` (+279)
- scope per plan Task 6 + §7.1: BestSource().source(input) in the vpy; source field-order property vs CLI field_order matched fail-closed before processing; preset registry PRESETS (raw={}, light={denoise_tr:1, BASIC}, strong={denoise_tr:2, REFINED}) as versioned immutable constants (raw = QTempGaussMC-defaults baseline, deinterlaced, NOT passthrough); unknown preset rejected pre-processing; build_qtgmc_clip (double = as-is, half = std.SelectEvery(cycle=2, offsets=0)); validate_qtgmc_output asserts frame count/rate/progressive/SAR before encode; vspipe --y4m --arg ... render_qtgmc.vpy - piped to ffmpeg stdin via Python Popen (shell=False, never a shell pipeline); FFV1 video-only <name>.partial.mkv; second ffmpeg pass -c:a copy of ALL original audio streams; contract (FFV1, yuv422p10le, progressive, exact rate, color/SAR/DAR, audio stream/codec/packet match) before publish; only PASS -> os.replace same volume, never overwrite valid artifact, stale .partial recorded interrupted.
- post-review fix (bac252e): vspipe stderr drained concurrently (daemon thread started before ffmpeg communicate, joined before exit-code checks, bounded join in cleanup) fixing a pipe-buffer deadlock risk on long renders; stale mux partial recorded interrupted + removed before the audio pass.
- PLAN DEVIATION: none.

### Task 6 review record (2026-09-21)

- reviewer: cursor/glm-5.2 (high, read-only)
- first pass (target: a16a2cd, parent 857352d): APPROVE_WITH_CONDITIONS - Critical 0 / Important 1 / Minor 3, behavior preservation PASS, fail-closed PASS, no scope deviation.
  1. (IMPORTANT) _run_vspipe_to_ffmpeg drained vspipe stderr only after ffmpeg communicate() -> pipe-buffer deadlock risk on multi-hour renders - fixed in bac252e with concurrent drain thread + bounded cleanup join + new bounded deadlock regression test.
  2. (MINOR) stale mux partial not recorded/removed before audio pass - fixed in bac252e (record interrupted + unlink(missing_ok=True), symmetric with video partial) + new test.
  3. (MINOR, accepted as-is) redundant preset_parameters validation call in build_vspipe_command - kept.
  4. (MINOR, accepted as-is) whole-file SHA-256 in capture_file_fingerprint - kept (TC-36 requires SHA-256 before/after).
- re-review (target: 857352d..bac252e): APPROVE - Critical 0 / Important 0 / Minor 0; IMPORTANT + stale-mux RESOLVED with file:line evidence; fix commit touched only the 2 in-scope files; behavior preservation PASS; fail-closed PASS; no scope deviations.
- fresh verification (coordinator, 2026-09-21):
  - not-vapoursynth gate: 11 passed, 1 deselected
  - vapoursynth gate: expected RED unchanged - 1 failed (native plugin missing), 11 deselected (-Apply still not approved)
  - regression: tests/test_manifest.py + tests/test_chunks.py -> 64 passed
