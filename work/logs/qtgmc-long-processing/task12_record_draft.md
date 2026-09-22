### Task 12 implementation record (2026-09-21/22)

- commits: a1ab594 (feat: verify and publish lossless long-form output), 0977bb7 (fix: harden Task 12 fail-closed behavior per review findings)
- files: e50618e..a1ab594 = pipeline/media_contract.py (+406), pipeline/run_local.py (+475), tests/test_long_media_contract.py (+836), tests/test_run_local.py (+32); net +1726/-23. 0977bb7 = 3 files, +174/-31.
- scope per plan Task 12 + TC-33..TC-36:
  - Final media contract (`verify_final_media_contract`, media_contract.py): FFV1 codec + yuv422p10le, frame count, avg_frame_rate equals selected rate, width/height/SAR/color fields match the source manifest fail-closed (no guessing; `color_transfer` maps the ffprobe `color_trc` key exactly), duration within `FINAL_DURATION_TOLERANCE` (0.5s), audio stream count/codec/packet counts, source + preprocessed immutability (`verify_file_unchanged`); report carries the 8-bit boundary warning (TAPE 8-bit PNG/OpenCV processing is not the same precision as the 10-bit FFV1 container).
  - Default finalization runners (run_local.py): `_default_concat_runner` (validates concat prerequisites — all chunks complete with valid sidecars — builds the concat listfile, ffmpeg concat demuxer to `output/concat.mkv`), `_default_remux_runner` (`ffmpeg -i concat.mkv -i source -map 0:v:0 -map 1:a -c:v copy -c:a copy` to `publish.final_mkv`), `_default_verify_runner` (writes run/chunk/reference manifest snapshots, runs `verify_final_media_contract`, publishes on PASS). Resolvers follow the `_resolve_qtgmc_runner` pattern: injected runner wins, else the real default, so CLI `run`/`resume`/`batch` execute real finalization (binding spec design #4).
  - Publish block (manifest `publish.*`): `final_mkv` / `handoff_json` / `run_manifest_snapshot` / `chunk_manifest_snapshot` / `reference_manifest_snapshot` under `work_root/output/<safe_stem>-<hash12>_*`; `public_dir` defaults to `<repo>/work/tape`. `_publish_verified_artifacts`: validates all five sources before writing anything, copies the four artifacts then the handoff json LAST (completion marker), atomic temp+`os.replace` per file, and on copy failure best-effort deletes already-written public destinations before re-raising (no partial public publish).
  - Handoff json: final file name/sha256, duration, avg_frame_rate, video fields from the probe (`color_transfer` from the `color_trc` key), audio stream index/codec/packet counts, warnings, source_sha256, run_id.
  - CLI `verify` subcommand: drives finalization+verify over completed stages; JSON report on stdout.
  - `resolve_chunk_output_path`: unified flat/production layout `tape/chunks/<chunk_index>/chunk.mkv` for chunk records.
- PLAN DEVIATION (recorded): the first implementation left concat/remux/verify injection-only (resolvers returned `None`), deviating from binding spec design #4. The coordinator caught this in verification and dispatched a fix agent before review close: resolver default wiring, the old pending-without-injection test replaced by `test_concat_remux_verify_resolve_default_runners_when_not_injected`, plus new e2e `test_tc33_run_executes_default_finalization_end_to_end` (real ffmpeg concat/remux/verify + real publish to a tmp public dir).
- Review-fix chain (0977bb7, per reviewer findings, see review record): F1 fail-closed color reads (no `color_space` fallbacks; `color_trc` key mapping only; `ffprobe_json` normalizes the FFmpeg 8.x `color_transfer` key to `color_trc` only when the value is actually present — no fabrication), F2 all-or-nothing publish (handoff last + cleanup on failure), F3 `test_tc35_final_media_contract_fails_on_missing_color_field`, F4 manifest `source.video` width/height fail-closed, F5 dead `_SAFE_STEM_RE` + unused `import re` removed. `_make_ffv1_source` fixture hardened (remux pass writes `-color_primaries`/`-color_trc` smpte170m) so FFV1 fixtures expose real color metadata under the fail-closed contract.
- Coordinator verification (2026-09-21/22):
  - Windows targeted (fresh basetemps): a1ab594 state — tests/test_run_local.py + tests/test_long_media_contract.py 75 passed; 0977bb7 state — test_long_media_contract + test_run_local + test_manifest + test_chunks 141 passed.
  - short_inference on the Windows host: 17 failures, all environmental (host ffprobe reports no `color_primaries` on the VHS_10s.mkv fixture; reproduces without this change; green under WSL).
  - Canonical WSL gates: task12_wsl.sh (long_media+short) 73 passed EXIT_CODE=0 (long_media 48 incl. e2e, short 25); task11_wsl.sh (run_local+manifest+chunks) 91 passed EXIT_CODE=0; post-0977bb7 gate: PENDING_FILL
- Residuals (non-blocking, accepted): references/chunk runners still require injection for a fully unattended CLI (spec scope — Task 12 covers finalization; unattended TAPE stages out of scope); default `public_dir` is the repo `work/tape` (tests override); pre-existing DAG manifests keep legacy publish filenames until re-analyze; preflight-side `color_transfer`/`color_trc` key asymmetry is pre-existing and fail-closed.

### Task 12 review record (2026-09-22)

- reviewer: independent reviewer subagent (read-only; harness equivalent of the established cursor/glm-5.2 high gate)
- review scope: e50618e..a1ab594 (Task 12 feature).
- first pass verdict: APPROVE_WITH_FIXES — Critical 0 / Important 2 / Minor 3:
  - F1 (Important, in-scope): `_final_video_field` color-space fallbacks could mask missing `color_primaries`/`color_transfer`.
  - F2 (Important, in-scope): sequential publish could leave a partial `public_dir`; the handoff json (completion marker) was copied second of five.
  - F3 (Minor): TC-35 had no dropped-color-field failure test.
  - F4 (Minor): width/height expected values tautologically defaulted to the final probe's own values when the manifest omitted them.
  - F5 (Minor): unused `_SAFE_STEM_RE`.
- fix: 0977bb7 (fresh fix agent, spec task12fix2_spec.md).
- re-review (scope a1ab594..0977bb7): APPROVE — Critical 0 / Important 0 / Minor 0. PREVIOUS_FINDINGS_RESOLUTION: F1-F5 all RESOLVED with file:symbol evidence; intentional extras verified fail-closed (`ffprobe_json` key compatibility never fabricates; fixture hardening only); no new findings.
- TEST_EVIDENCE: PENDING_FILL (post-fix canonical WSL gates + Windows targeted 141 passed).
- SCOPE_DEVIATIONS: none — a1ab594 touches exactly the four in-scope files; 0977bb7 exactly three.
- PLAN DEVIATION: the initial injection-only resolver wiring (see implementation record) was fixed and re-reviewed before close; no requirement dropped.
