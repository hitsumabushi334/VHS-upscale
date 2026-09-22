# QTGMC・長時間ローカル処理 — 実装記録

> この文書は実装計画書 `docs/qtgmc-long-processing/implementation_plan.md`(最終承認済み)の実装記録である。
> baseline・既知制約・各Taskの実測・レビュー記録をここに追記する。

## 1. baseline (Task 0)

- worktree: `E:\vhs-upscale\worktrees\tape-qtgmc-long-processing`
- branch: `codex/tape-qtgmc-long-processing`
- base commit: `c4ee9aae6e95ab257e3b3e4b538e6c20e1b7c9dc` (upstream base `fb97c608a1733dec5dbcd5aeb77ed84e29b793f7`)
- 開始時dirty: なし
- 詳細JSON: `E:\vhs-upscale\work\local-pipeline\baseline.json`(Git外)

### 1.1 既存テストbaseline (2026-09-20, fresh)

新worktreeは `vapoursynth`/`e2e` markerをまだ持たないため、既存suiteは旧markerでWSL venv Python(luna worktreeの同一commit venv)で実行。GPU群はRX 7900 XTをWSL経由で使う。

| group | result | log |
|---|---|---|
| unit (`not gpu and not integration and not e2e`) | 99 passed, 28 deselected | `work/logs/qtgmc-long-processing/task0-newworktree-unit.log` |
| integration | 25 passed, 102 deselected | `work/logs/qtgmc-long-processing/task0-newworktree-integration.log` |
| gpu | 3 passed, 124 deselected | `work/logs/qtgmc-long-processing/task0-newworktree-gpu.log` |

予期しないskip: 0件。

### 1.2 Windows環境

- Windows 10 Home 25H2
- Python 3.13.1 (`C:\Python313\python.exe`)
- VapourSynth 79.0、vsjetpack 2.2.4 (pip, Python 3.13)
- vspipe: `C:\Users\issho\AppData\Roaming\Python\Python313\Scripts\vspipe.exe`
- FFmpeg/ffprobe 8.1.1 (WinGet Gyan build)
- WSL2: `Ubuntu-22.04` (running)

### 1.3 VapourSynth probe (Task 4 REDの根拠)

- `vsdeinterlace` import: OK、`QTempGaussMC` Python symbol: 存在
- `vssource.BestSource`: 存在
- synthetic BlankClipのfirst frame request: OK
- `QTempGaussMC().deinterlace(...).get_frame(0)`: **FAIL** — `No attribute with the name resize2 exists...`
- 判定: Pythonパッケージは揃っているがQTGMC実行に必要な**native pluginが欠如**。計画が想定する `missing_dependency` の期待されるRED状態。

### 1.4 asset hash (assets.jsonと一致)

| asset | sha256 | size |
|---|---|---|
| TAPE checkpoint | `14f89686d202a52bf14f0a18e9b7eadf617bf4c2425805a3d8ccba4351727a3d` | 378,230,179 |
| CLIP RN50x4 | `7e526bd135e493cef0776de27d5f42653e6b4c8bf9e0f653bb11773263205fdd` | 421,854,225 |
| Swin-T | `704ceda373461b0a224fcdddd75cd2a5e9f8064512ed47adbddef7f343fd147b` | 113,445,839 |

### 1.5 既知制約 (隠さず記録)

1. **1280x720 full-resolution TAPEはfail closed (HIP OOM)**。TAPE ROCm計画Task 8の実測。
2. **704x396実測**: 297 frames / 282.633 s / 1.051 fps、peak allocated 8.81 GiB / reserved 14.56 GiB (AMP/autocast)。
3. **native QTGMC plugin欠如**: preflightはfail closedでなければならない。Task 4でpinned extras経由で復旧する。
4. **29.97i VHS sourceが未到着**: `input/`はprogressive素材のみ。Task 14/16のゲート。
5. **Windows Pythonにはtorchなし**: TAPE推論本体はWSL venv (`tape-qtgmc-long-processing/.venv-rocm`, Task実行時に構築)で実行する。
6. **CRLF checkout事故**: 新worktree作成時、グローバル `core.autocrlf=true` によりtracked LF filesがCRLF化され、WSL bashで64件のテストがfailure。working treeをblobとbyte一致に書き直し、worktreeローカル `core.autocrlf=false` で解消。再現防止のためworktreeローカル設定を維持する。

## 2. Task実装記録

(各Task完了後に追加)


### Task 1 review record (2026-09-20)

- reviewer: cursor/glm-5.2 (high, read-only)
- target: commit 8e546b6..a9116e2 (Task 1, 4 files)
- verdict: APPROVE - Critical 0 / Important 0 / Minor 3, plan deviation: none
- Minor points:
  1. safe stem regex trailing end anchor is redundant with fullmatch() (cosmetic, no behavior change)
  2. _absolute_wsl_path isinstance check vs type annotation mismatch (defensive, tolerated)
  3. windows_to_wsl passes traversal segments through; covered by Task 12 trusted-root checks (out of Task 1 scope)
- Action: no fix required. Point 3 kept as a Task 12 review checkpoint.

### Task 2 review record (2026-09-20)

- reviewer: cursor/glm-5.2 (high, read-only)
- first pass: conditional - Critical 0 / Important 2 / Minor 3 (empty-artifact complete skip not test-protected; chunk-level running reconciliation missing)
- fix commit: 853bea9 (3 new tests, dependency table source.overrides row, annotation fix, TC-18 error asserts)
- re-review: APPROVE - Critical 0 / Important 0 / Minor 0, all 5 prior points resolved
- manifest suite: 34 passed (27 + 7 new)

### Task 3 implementation record (2026-09-20)

- commit: `04dd3ba` `feat: add deterministic frame chunk planner`
- files: `pipeline/chunks.py` (+130), `tests/test_chunks.py` (+152)
- TDD: RED (`ModuleNotFoundError: No module named 'pipeline.chunks'`) → GREEN 30 passed
- TC-11..TC-14 covered; `plan_chunks` fail-closed validation (`ValueError`, no auto-correction); `validate_partition` as a separate validator; `seconds_to_frames` uses Fraction/Decimal with round-half-up (no float)
- fresh verification (coordinator): `python.exe -m pytest tests/test_chunks.py tests/test_manifest.py` → 64 passed

### Task 3 review record (2026-09-20)

- reviewer: cursor/glm-5.2 (high, read-only)
- target: commit 04dd3ba (Task 3, 2 files)
- verdict: APPROVE - Critical 0 / Important 0 / Minor 1, plan deviation: none
- Minor points:
  1. `_round_half_up` defensive negative-value branch is unreachable for plan-specified non-negative seconds/fps inputs (no behavioral impact; recorded for the record)
- Action: no fix required.

### Task 4 implementation record (2026-09-20)

- commits: `d1dbe0b` `build: pin Windows VapourSynth QTGMC environment` + `5062017` `fix: add BestSource preflight to vapoursynth checker`
- files: `requirements-vapoursynth.txt`, `scripts/install_vapoursynth.ps1`, `scripts/check_vapoursynth.py`, `pytest.ini` (vapoursynth marker), `tests/test_qtgmc_contract.py`
- pinned: `--extra-index-url https://jaded-encoding-thaumaturgy.github.io/vs-wheels/simple`, `vapoursynth==79`, `vsjetpack[deinterlace,source]==2.2.4`
- installer: default=dry-run, `-Apply` only for real install, command+exit_code logged (no uninstall/registry change). dry-run executed (no `-Apply` run).
- checker (TC-01): VS/Python version, vspipe+ffmpeg path, plugin inventory, QTempGaussMC synthetic first-frame request, BestSource short-fixture open (FFmpeg argument-list, temp fixture). Failure path: `status=missing_dependency` + missing namespace/function + inventory + recovery command + exit 1.
- expected environment RED (fresh): `python scripts/check_vapoursynth.py --json` → exit 1, `failure_stage=first_frame_request`, `missing_native_dependency.namespace=resize2` (native plugin missing). This is the plan's expected RED.
- unit tests: failure-path reporting for import/frame/BestSource (no real plugin dependency). vapoursynth-marked full-preflight test is RED in the current environment (expected).

### Task 4 review record (2026-09-20)

- reviewer: cursor/glm-5.2 (high, read-only)
- first pass: CONDITIONAL - Critical 0 / Important 1 (BestSource preflight missing) / Minor 1 (installer log BOM).
- fix commit: `5062017` (added `open_short_fixture_bestsource`, BOM-free UTF-8 log write, new unit test).
- re-review: APPROVE - Critical 0 / Important 0 / Minor 0, both prior points resolved.
- fresh verification (coordinator): unit `3 passed, 1 deselected`; checker exit 1 with resize2 RED; dry-run logs BOM-free with command+exit_code.

### Task 4 environment caveat (for `-Apply`)

- The most recent dry-run (`install-vapoursynth-20260920-202030.log`) exited 1: `dfttest2>=10 ... (from versions: none)`. An earlier dry-run (`...195057.log`) resolved dfttest2 from cache and exited 0.
- Interpretation: resolving the native plugins (dfttest2, resize2, etc.) requires network reachability to the vs-wheels extra index. `-Apply` must therefore be run with network access (sandboxed/escalated). The pinned requirements are correct per plan; only the live index lookup is the variable.
- `-Apply` is still gated on explicit user approval and has NOT been run.


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

### Task 8 implementation record (2026-09-21)

- commits: 606813b (feat: add video-wide resumable reference cache), d87f047 (fix: declare utils as a regular package)
- files: pipeline/references.py (+925), utils/reference_selection.py (+118), tests/test_reference_cache.py (+384), real_world_inference.py (+324/-113 refactor), utils/__init__.py (+7)
- scope per plan Task 8 + TC-22..27: real_world_inference.py split into extract_video_frames / select_references / restore_frames with no behavior change first; prompt ensemble, CLIP normalize, and Otsu clean score moved to pure numpy helpers (resolve_reference_names and empty-pool fallback stay short mode in utils/real_world_helpers.py); module imports cleanly without torch/clip/skimage on Windows; frame_bytes_to_pil_rgb rejects wrong-length rgb24 bytes and returns PIL RGB with no BGR conversion; in-order FFmpeg rawvideo pipe decode (argument list, trim=start_frame:end_frame,setpts=PTS-STARTPTS, -pix_fmt rgb24) with no OpenCV seek or CAP_PROP_FRAME_COUNT; 10,000-frame feature shards (range/shape/dtype/hash/status; whole-shard recompute on failure, no intra-shard resume in v1); pool = timeline bins with min clean score per bin (tie -> smaller index), global-ranking fill only when short, zero candidates -> reference_failed stop without the short fallback; only pool frames saved as 8-bit PNG; per-target-shard cosine vs max-256 pool, top D per target, tie -> smaller global index, 9-digit names, exactly D refs (pad by repeating top); mapping validated before cache marked complete; cache key from canonical JSON of video SHA-256, frame count/rate, preset/rate selection, CLIP model SHA-256, prompt set version, feature dtype, pool algorithm version, pool size, reference count (no absolute path, no run timestamp).
- coordinator fix (d87f047): Windows collection of the utils.* suites failed because the repo utils/ namespace directory was shadowed by a same-named module in user site-packages (pre-existing environmental issue, file dates 2026-07-25); adding utils/__init__.py makes it a regular package. No behavior change to existing utils modules.
- PLAN DEVIATION: none (TC-26 chunk-integration parts left to Task 9 by plan).

### Task 8 review record (2026-09-21)

- reviewer: cursor/glm-5.2 (high, read-only)
- review scope: 606813b + d87f047 as one logical change.
- verdict: APPROVE - Critical 0 / Important 0 material / Minor 2.
  - (IMPORTANT observation, accepted) otsu_threshold replaces skimage.filters.threshold_otsu with pure numpy (bins=256, range min..max, matching skimage float path); pathological bin-boundary ties could differ by one bin width; sanctioned by plan (pure helper move) and short-mode tests green.
  - (MINOR) new empty-frames guard in select_references (ValueError instead of later NameError) - behavior improvement, not a regression.
  - (MINOR) pool fill repeats the single best candidate when candidates are very scarce - documented pad-by-repeating-top rule, exercised by test.
  - BEHAVIOR_PRESERVATION: refactor verified against 606813b^ - same stage order, numerically equivalent prompt ensemble/normalize, identical Otsu index selection, identical reference loop and restore pipeline; module-level torch/clip/skimage imports deferred into stages.
  - FAIL_CLOSED: zero-pool ReferenceFailed with short fallback asserted never called; partial/short/extra rawvideo reads and ffmpeg nonzero exit raise; shard/pool/mapping corruption re-hash and rebuild only the corrupt scope; build_mapping validates before status complete; validate_chunk_request rejects mutated key/manifest hash/root.
  - TEST_EVIDENCE: canonical WSL gate (task8-wsl-20260921.log): tests/test_reference_cache.py + tests/test_real_world_helpers.py + tests/test_inference_contract.py -> 26 passed, EXIT_CODE=0; real_world_inference imports without torch (PASS). Windows: suites collect after d87f047; torch-importing paths remain expected-red on this host.
  - SCOPE_DEVIATIONS: none. d87f047 is docstring-only and behavior-neutral; no files outside the write set touched.

### Task 9 implementation record (2026-09-21)

- commit: 5aaf5d9 (feat: let TAPE consume shared external references)
- files: real_world_inference.py (249 changed), data/real_world_video_dataset.py (74 changed), tests/test_tape_chunk.py (+196 new), tests/test_inference_contract.py (72 changed), tests/test_short_inference_contract.py (8 changed); total +550/-49
- scope per plan Task 9 + section 7.5 + TC-28/TC-29: five external CLI args (--input-frames-path / --reference-map-path / --reference-frames-path / --skip-reference-selection / --global-start-frame); one-time all-or-nothing mode determination at process start (partial set is an argparse parser error, SystemExit 2, before any stage); external mode never calls extract_video_frames or select_references (no cv2.VideoCapture, no clip.load, no Otsu) and passes external input/mapping/reference roots to restore_frames only; validate_external_inputs fail-closes before any model/checkpoint load on missing mapping key, missing reference PNG, fewer than D references, input frame-name mismatch, and empty reference pool (ValueError); RealWorldVideoDataset gained reference_frames_folder (default None keeps input folder, behavior unchanged) with fail-closed validation in __init__ and torch import deferred into __getitem__ (dataset no longer subclasses torch.utils.data.Dataset; DataLoader still works via __getitem__/__len__); short mode keeps the conventional decode/select/padding route unchanged.
- implementation note: the first implementation agent errored mid-work (weekly usage limit); the coordinator carried the remaining work: verified the residual change, added a repo-root-relative tmp fixture because built-in pytest tmp_path hit WinError 5 on Windows (same pattern as Task 8), and committed.
- PLAN DEVIATION: none.

### Task 9 review record (2026-09-21)

- reviewer: independent reviewer subagent (read-only; harness equivalent of the established cursor/glm-5.2 high gate)
- review scope: 22f5674..5aaf5d9 (effectively commit 5aaf5d9 only)
- verdict: APPROVE - Critical 0 / Important 0 / Minor 0.
  - BEHAVIOR_PRESERVATION: PASS. Short path still VideoCapture -> extract_video_frames -> select_references -> restore_frames without reference_frames_folder; black-frame padding loop unchanged; external restored PNGs keep 9-digit global names; module imports without torch/clip/skimage/pytorch_lightning.
  - FAIL_CLOSED: PASS. Partial external CLI set -> parser SystemExit 2; the four TC-29 errors (missing mapping key, missing reference PNG, fewer than D refs, name mismatch) plus empty pool raise ValueError in validate_external_inputs before restore_frames/torch.load; no fallback to CLIP/Otsu/decode.
  - TEST_EVIDENCE: additive only; no assertion removed, loosened, or skipped. New tests cover complete/partial parser, extract/select skip via raising mocks, four fail-closed cases, short-mode regression, and short-runner argv. Note: the fail-closed test accepts (ValueError, FileNotFoundError, KeyError) while production raises ValueError (broader than plan wording, coverage not dropped).
  - SCOPE_DEVIATIONS: none (git diff --name-only 22f5674..5aaf5d9 is exactly the five in-scope files).
  - PLAN DEVIATION: none that break required section 7.5/Task 9/TC-29 behavior. Residuals (non-blocking): --global-start-frame participates in the presence check but its value is not compared to the first 9-digit frame name (format is still enforced); external VideoWriter fps is hardcoded 30.0; dataset no longer subclasses torch.utils.data.Dataset (required to drop top-level torch); dataset __init__ fail-closure now applies in short mode too (generated short mappings still match *.png inputs).
- TEST_EVIDENCE (coordinator): canonical WSL gate (task9-wsl-20260921.log): tests/test_tape_chunk.py + tests/test_inference_contract.py + tests/test_short_inference_contract.py -> 45 passed, EXIT_CODE=0, 42.11s. Windows (fresh basetemp): test_tape_chunk.py 11 passed; test_reference_cache + test_real_world_helpers + test_tape_chunk 30 passed. Direct scripts: import without torch OK; partial external sets all parser-error; 4 fail-closed cases raise before model load (model_reached: []).

### Task 10 implementation record (2026-09-21)

- commits: 850e8cc (feat: add resumable TAPE chunk worker), 4e29079 (fix: bound chunk encode input and platform-neutral WSL argv), ceb7c72 (fix: TAPE chunk publish safety, explicit CFR/SAR, retry cleanup), f0883ca (fix: accept omitted color probes, colon-safe SAR compare, best-effort cleanup)
- files: pipeline/tape_chunk.py (new, +546), pipeline/paths.py (+19, to_wsl_arg), tests/test_tape_chunk.py (+732 on the Task 9 file); total a55e731..f0883ca = +1297/-0
- scope per plan Task 10 + section 7.6 + TC-28/TC-30/TC-31:
  - extract_chunk_frames: `-vf trim=start_frame=S:end_frame=E,setpts=PTS-STARTPTS` + `-start_number S` (no `-ss` timestamp seek), global 9-digit PNG names, exact count/name verification (TC-28).
  - trim_to_core: restored PNGs for the core range only; concatenated chunks cover exactly 0..N-1 (TC-30).
  - encode_chunk_ffv1: bounded input (`-framerate <tape_fps> -start_number <first> -i <dir>/%09d.png -frames:v <core_count>`), `-c:v ffv1 -level 3 -g 1 -slicecrc 1 -pix_fmt yuv422p10le`, media color flags via _COLOR_FLAG_MAP (per-flag dedupe), `-fps_mode cfr`, `-an`, partial output.
  - _validate_partial_against_request: ffv1 codec, yuv422p10le, frame count, avg_frame_rate, width/height, color fields (present probe values must match; omitted probe keys are the accepted probe-instability residual), SAR compare as colon-safe Fraction (ffprobe "N:M" form).
  - run_tape_chunk: shell-free WSL argv via build_wsl_argv (wsl.exe -d <distro> -- <python> <script> args; `bash -lc` prohibited), TAPE external reference mode args + `--preprocess-mode none` (matches short runner; the default crop@512 breaks sources wider than 512px), stale stem-dir cleanup before WSL, overwrite refusal (existing chunk.mkv/chunk.json -> already_published, retryable=False, before any work), publish only after probe + ffmpeg version + metrics validation (metrics must be a JSON object), atomic sidecar (tmp + os.replace), categorized failure diagnostics (stderr tail 2000 chars, wsl.log) without publishing.
  - to_wsl_arg (pipeline/paths.py): Windows absolute mapped-drive path -> /mnt/<drive>/..., absolute POSIX pass-through, anything else ValueError.
- PLAN DEVIATION: none. encode_chunk_ffv1 gains tape_fps/ffmpeg/runner parameters relative to the section 7.6 stub (required for argv construction); --tape-worktree is converted to the absolute script path per the section 7.6 example.
- coordinator verification (2026-09-21): WSL canonical gate (task10-wsl-20260921-6.log): tests/test_tape_chunk.py + tests/test_inference_contract.py + tests/test_short_inference_contract.py -> 62 passed, EXIT_CODE=0; Windows fresh basetemp: test_tape_chunk.py 28 passed; manifest+chunks+run_local+tape_chunk 102 passed. Pre-existing Windows-only failure test_qtgmc_contract.py::test_tc01 confirmed on clean tree 4e29079 (environment-dependent, out of Task 10 scope). Direct probe with the exact worker argv on FFmpeg 8.1.1 (Gyan): color_range/color_space present, color_primaries/color_transfer omitted, sample_aspect_ratio "1:1" persisted by -sar, codec/pix_fmt/count/rate contract fields present.

### Task 10 review record (2026-09-21)

- reviewer: independent reviewer subagents (read-only; harness equivalent of the established cursor/glm-5.2 high gate). Three reviews across the fix chain: initial review of the feature commits, re-review of a55e731..ceb7c72, final review of ceb7c72..f0883ca.
- review chain: initial review returned conditional (CRITICAL: _build_wsl_args omitted --preprocess-mode none; IMPORTANT: publish preceded metrics/ffmpeg version/sidecar validation; IMPORTANT: stable work_dir + TAPE mkdir(stem, exist_ok=False) race) plus advisory items (explicit -fps_mode cfr, SAR compare, atomic sidecar, overwrite refusal). Fix commit ceb7c72 addressed all. Re-review of a55e731..ceb7c72 returned APPROVE_WITH_CONDITIONS: Critical 1 (FFV1/MKV ffprobe on FFmpeg 8.1.1 omits color_primaries/color_transfer, so the present-value color compare fail-closed every real chunk; unit mocks hid it), Minor 1 (Fraction("1:1") raises ValueError - colon form invalid). Fix commit f0883ca addressed both.
- final verdict (ceb7c72..f0883ca): APPROVE - Critical 0 / Important 0 / Minor 0.
  - PREVIOUS_FINDINGS_RESOLUTION: both resolved with file:symbol evidence; None-skip is absence-only (present values still must match), dedupe leaves normal media argv unchanged, try/except additions do not mask failures (leftover orphans still hit already_published).
  - BEHAVIOR_PRESERVATION: PASS. Codec/pix_fmt/count/rate/width/height checks, present-color mismatch, SAR numeric mismatch, already_published, and no-publish-on-contract-mismatch unchanged.
  - FAIL_CLOSED: PASS. No fail-open path introduced.
  - TEST_EVIDENCE: additive only (4 new tests vs ceb7c72, no assertions removed); reviewer's targeted Windows run: 7 passed in 0.43s. Canonical WSL gate after fixes: 62 passed, EXIT_CODE=0.
  - SCOPE_DEVIATIONS: none. a55e731..f0883ca touches only pipeline/tape_chunk.py, pipeline/paths.py, tests/test_tape_chunk.py.
  - PLAN DEVIATION: none breaking Task 10/TC-31. Residuals (non-blocking, accepted): omitted-probe skip covers all four color keys (empirically only primaries/transfer are dropped on 8.1.1); conflicting sar vs sample_aspect_ratio media keys are first-wins in the encode argv; a failed wsl.log write is silent while the sidecar still names stderr_log=wsl.log; omitted probed SAR remains skipped (consistent with the color residual).
  - process note: the first final-review instance stalled and was cancelled after delivering its structured verdict via IRC; a fresh re-spawn was started and cancelled as redundant once the verdict was in hand.

### Task 11 implementation record (2026-09-21)

- commits: c75c0f1 (feat: orchestrate resumable local TAPE runs), b43616d (fix: remove dead orchestrator duplicates, header imports, self-contained liveness check), 140bfdf (fix: use Python 3.10-compatible Enum for StageStatus), 318341a (fix: harden Task 11 fail-closed behavior per review findings), fcaa126 (fix: resume retries pending chunks after tape_chunks invalidation)
- files: pipeline/run_local.py (+1163/-4), tests/test_run_local.py (+687/-1), pipeline/manifest.py (2+/2-, Python 3.10 Enum compatibility)
- scope per plan Task 11 + section 7.7 + TC-17..TC-21, TC-32, TC-39:
  - _run_pipeline: stage execution loop with injected runners; run mode skips complete stages via decide_stage; resume continues from incomplete stages; tape_chunks retry set is all non-complete chunks, unified for run and resume (fcaa126).
  - CLI: canary / run / resume / verify subcommands and --batch; --fault-inject-chunk hidden by default (argparse.SUPPRESS) and requires PIPELINE_ENABLE_FAULT_INJECTION=1 for both CLI and the Python API (_enforce_fault_injection_gate).
  - TC-17: completed stage with valid cache is skipped (test_tc17_completed_stage_with_valid_cache_is_skipped).
  - TC-18: corrupted artifact invalidates downstream stages and is quarantined (test_tc18_corrupted_artifact_invalidates_downstream_and_quarantines).
  - TC-19: _reconcile_manifest_changes - selection diff (preset/rate) invalidates from the dependency table; assets/source sha diff invalidates all; canary signature stamped only after qtgmc complete + contract_passed (else canary=None) and run() rejects a mismatched signature.
  - TC-20: reconcile_source - mtime-only change is a warning; hash change invalidates all stages.
  - TC-21: resume retries only the failed chunk (chunk_calls == [2], complete siblings' hash/mtime unchanged); fault injection is env-gated and recorded (fault_injected flag).
  - TC-32: _gpu_lock - atomic O_EXCL acquisition with a bounded 8-try stale-lock replacement loop and self-contained liveness check (_default_is_live_pid; Windows fail-closed to live=True when liveness cannot be confirmed); raises when the lock cannot be acquired.
  - TC-39: batch isolates per-source failures and namespaces work dirs (test_tc39_batch_isolates_failures_and_namespaces); the report uses a stable per-stage view (_stable_dag_stage_view).
- PLAN DEVIATION: the first agent submission contained dead duplicated orchestrator code (removed in b43616d) and a Python 3.13-only StageStatus Enum (fixed in 140bfdf for WSL Python 3.10). Both are recorded as fix commits; no requirement was dropped.

### Task 11 review record (2026-09-21)

- reviewer: independent reviewer subagent (read-only; harness equivalent of the established cursor/glm-5.2 high gate). Three-pass chain: initial review (REJECT, 7 findings) -> re-review after 318341a (REJECT: 1 Critical + 1 partial Important) -> final delta review of fcaa126 (APPROVE).
- finding resolution:
  - F1 (CRITICAL -> FIXED in fcaa126): resume()'s tape_chunks retry set used resumable_chunks() (failed/interrupted/invalid only); after a selection/CLIP change the invalidation resets chunks to pending, so resume re-executed 0 chunks and marked tape_chunks complete with null hashes (reviewer reproduced it). Fixed by unifying the non-complete retry set for run and resume; test_tc19 now asserts chunk_calls == [0, 1, 2, 3] after both the preset-change resume and the CLIP-change resume.
  - F2 (FIXED in 318341a): canary signature stamped only after qtgmc complete + contract_passed; else canary=None.
  - F3 (FIXED in 318341a): _default_is_live_pid fail-closed on Windows (OpenProcess failure / GetExitCode failure / exception -> True).
  - F4 (ACCEPTED for selection/assets; declared residual for config-only): selection/assets/source-version diffs auto-invalidate via the dependency table; config.* changes are not watched.
  - F5 (FIXED in 318341a): --fault-inject-chunk defaults to argparse.SUPPRESS; the env gate is enforced for the Python API as well.
  - F6 (PARTIAL -> FIXED in fcaa126): test_tc19 drives resume() after the change and asserts chunk re-execution in both phases.
  - F7 (FIXED in 318341a): _gpu_lock stale replacement is a bounded 8-try O_EXCL loop; raises when the lock cannot be acquired (no silent fallback).
- TEST_EVIDENCE: canonical WSL gate (task11-wsl-final-20260921.log): tests/test_run_local.py + tests/test_manifest.py + tests/test_chunks.py -> 91 passed, EXIT_CODE=0. Windows fresh basetemp after fcaa126: tests/test_run_local.py + tests/test_manifest.py -> 61 passed; WSL re-run after fcaa126 (task11-wsl-fix2-20260921.log): 91 passed, EXIT_CODE=0.
- SCOPE_DEVIATIONS: none. Task 11 touches only pipeline/run_local.py, tests/test_run_local.py, and pipeline/manifest.py (Enum compatibility only).
- PLAN DEVIATION: none breaking Task 11/TC-17..TC-21/TC-32/TC-39 behavior. Residuals (non-blocking, accepted): config-only manifest changes are not watched (selection/assets/source diffs do invalidate); TC-39 compares the stable per-stage view rather than raw updated_at; resume short-circuits a COMPLETE tape_chunks stage when resumable_chunks() is empty - a corrupt COMPLETE+pending mix is not produced by the current invalidation path.
- process note: the final delta review ran on the same independent reviewer instance (Task11Review2) that performed the re-review, after a convergence request; verdict APPROVE with per-finding file:symbol evidence and TC-17/TC-21 regression checks.

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
  - Canonical WSL gates: task12_wsl.sh (long_media+short) 73 passed EXIT_CODE=0 (long_media 48 incl. e2e, short 25); task11_wsl.sh (run_local+manifest+chunks) 91 passed EXIT_CODE=0; post-0977bb7 (task12fix2_wsl.sh, 2026-09-22): gate 1 long_media+short 75 passed EXIT_CODE=0; gate 2 run_local+manifest+chunks 91 passed EXIT_CODE=0.
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
- TEST_EVIDENCE: post-fix canonical WSL gates green (75 + 91 passed, EXIT_CODE=0); Windows targeted 141 passed (fresh basetemp); reviewer targeted inspection read-only.
- SCOPE_DEVIATIONS: none — a1ab594 touches exactly the four in-scope files; 0977bb7 exactly three.
- PLAN DEVIATION: the initial injection-only resolver wiring (see implementation record) was fixed and re-reviewed before close; no requirement dropped.

## Operator guide: long-run estimates

Use the local pipeline CLI to inspect long-run timing and disk requirements before and during a full run:

```text
python real_world_inference.py estimate --work-root <work_root>
python real_world_inference.py status --work-root <work_root>
```

Both commands read the preview `manifest.json` under `<work_root>`. `estimate` recomputes the report, persists it under the manifest `estimate` key, and prints JSON; `status` includes the last persisted estimate block.

### Report fields

- **`tape_fps`**: Throughput inferred from completed TAPE chunk timings. The `label` is `measured` only when completed chunks exist **and** the source resolution equals the 704×396 benchmark resolution. At other resolutions a numeric value may still appear with `label` other than `measured` (never present an unmeasured number as measured). When no chunk timings exist, `value` is `null` and the label is not `measured`.
- **`predicted_tape_time` / `predicted_tape_time_seconds`**: `input_frame_count / tape_fps` when a tape fps value exists. The nested `label` is `measured` at the benchmark resolution, `rough_extrapolation` when extrapolating from non-benchmark timings, or `estimate` when unmeasured.
- **`qtgmc_fps` / `qtgmc_measured`**: Measured only after the QTGMC stage completes; otherwise treated as an estimate with no fabricated measurement.
- **`eta_seconds`**: Arithmetic mean of `wall_time_seconds` over **completed** chunks multiplied by the **pending** chunk count. Absent (`null`) until at least one chunk completes.
- **`chunk_counts`**: Completed, failed, and pending chunk counts from the DAG `tape_chunks` stage (pending requires a DAG manifest).
- **`ffv1_free_space_margin`**: Compares free bytes to the FFV1 requirement (measured extrapolation from completed chunk files when available, otherwise a conservative 2-bit/pixel upper bound).
- **`png_worst_case`**: Conservative RGB24 PNG upper bound per frame: `width * height * 3 + width + 64`, summed over all frames.

### Disk policy (fail closed)

Before `run`, `resume`, `canary`, or `batch` executes any stage, the pipeline checks:

```text
required = ffv1_required_bytes + png_worst_case_bytes
safe_required = ceil(required * 1.20)   # 20% safety margin
```

If free space under the work root is below `safe_required`, the pipeline raises `InsufficientDiskError` and does not create any artifacts.

### During a long run

Re-run `status` and `estimate` to refresh chunk counts, ETA, and disk margin. Watch for rising `chunk_counts.completed`, a stabilizing `eta_seconds`, and sufficient `ffv1_free_space_margin.free_bytes` relative to `required_bytes`.


### Task 13 implementation record (2026-09-22)

- commits: 4cfa7c0 (feat: add long-run estimates, ETA, run-start disk gate, and operator guide), 86bd38f (fix: run disk gate before DAG manifest bootstrap and lock margin in tests), 6ef6c9b (fix: batch performs no manifest write before the run-path disk gate)
- files: 4cfa7c0 = pipeline/run_local.py (+414), tests/test_run_local.py (+122), docs/qtgmc-long-processing.md (+37); net +571/-2. 86bd38f = 3 files, +59/-19. 6ef6c9b = pipeline/run_local.py + tests/test_run_local.py, +60/-7.
- scope per plan Task 13 + TC-37/TC-38:
  - Per-chunk wall time: tape_chunks stage chunk records now carry `wall_time_seconds` from the chunk runner result.
  - `estimate(work_root, disk_usage_fn=None)` (run_local.py, added to `__all__`): loads the preview manifest (RunLocalError if missing), selection JSON (selected preset + `tape_input_rate`), and the DAG manifest when present. Builds the TC-37 report via `build_estimate_report` with: `input_frame_count` from `source.frame_count`; `selected_fps` from the selection `tape_input_rate`; `tape_fps` measured as total core frames / total `wall_time_seconds` across completed chunks (sidecar-derived `core_end - core_start` when the record lacks it), `None` without usable chunk timings; `qtgmc_wall_time_seconds` from the COMPLETE qtgmc stage metrics. Top-level additions (existing fields untouched): `source_duration_seconds`, `selected_preset`, `qtgmc_fps` (measured/estimate label), `chunk_counts` {completed, failed, pending} (pending = planned - completed - failed; None when the DAG manifest is absent), `eta_seconds` = arithmetic mean of completed chunk wall times x pending (None until a chunk completes), `measured_tape_resolution`, `feature_cache_bytes` (measured under the reference cache root, else None), `log_bytes` (measured `*.log` under work_root). `predicted_tape_time` gains a `label`: `measured` only at the 704x396 benchmark resolution with completed chunk timings, `rough_extrapolation` when a value exists at another resolution, `estimate` when None. Disk formulas shared with the gate: `png_worst_case_bytes = frame_count * (w*h*3 + w + 64)`; `ffv1_required_bytes` = measured extrapolation `sum(chunk file bytes)/sum(core frames)*frame_count` when completed chunks exist, else conservative `frame_count*w*h*3//4`. Report persisted into the preview manifest under `"estimate"` (atomic write + `updated_at`); `status()` unchanged (already surfaces `manifest.get("estimate")`).
  - Run-start disk gate: `_enforce_disk_gate` at the start of `_run_pipeline`, BEFORE the DAG manifest bootstrap (`_ensure_dag_manifest`) and before any stage/canary — gate inputs come from the preview manifest plus an already-existing DAG manifest (read-only; conservative FFV1 formula when absent): `required = ffv1_required + png_worst_case`, `safe_required = ceil(required * 1.20)`, raise `InsufficientDiskError` when `free < safe_required`. `disk_usage_fn=None` (default `shutil.disk_usage`) is threaded through `run` / `resume` / `canary` / `batch` signatures, mirroring the existing injectable `analyze` gate (kept green). `batch()` performs no manifest-persisting write before the gate (read-only DAG pre-load; entry `run_id`/`manifest_path`/`reference_cache_root` are filled only after a successful `run`).
  - CLI `estimate` subcommand: `python real_world_inference.py estimate --work-root <root>`; prints the report as JSON (same output style as `status`); returns 0; RunLocalError/InsufficientDiskError print a JSON error report and return 1 (mirrors `analyze`).
  - Operator guide: new docs section `## Operator guide: long-run estimates` (English): estimate/status usage, field meanings (the measured/rough_extrapolation/estimate label contract), ETA semantics, disk policy (required = FFV1 + PNG worst case, +20% margin, fail-closed before artifacts), and what to watch during a long run.
- New tests (tests/test_run_local.py, TC-37): `test_tc37_estimate_unmeasured_without_chunks`, `test_tc37_estimate_measured_with_completed_chunks` (also asserts `wall_time_seconds` on the chunk record and `predicted_tape_time.label == "measured"`), `test_tc37_estimate_other_resolution_rough_extrapolation`, `test_tc37_run_stops_before_artifacts_when_disk_low` (locks the gate threshold to `ceil((ffv1+png)*1.20)` with boundary checks: threshold-1 raises, threshold passes; tightened post-condition), `test_tc37_estimate_cli_prints_json` (asserts `predicted_tape_time` dict, `eta_seconds` key, `input_frame_count`), `test_tc37_batch_disk_gate_creates_no_dag_on_first_batch` (first batch in the [analyze-threshold, run-threshold) window fails with `InsufficientDiskError`, no DAG manifest, no stage artifacts, no runner calls). Existing green tests kept: `test_estimate_report_never_calls_other_resolution_tape_fps_measured`, `test_insufficient_disk_stops_before_creating_artifacts`, the `status` test.
- Coordinator verification (2026-09-22):
  - Windows targeted (fresh basetemps): 96 passed at 4cfa7c0; 96 passed at 86bd38f; 97 passed at 6ef6c9b.
  - Canonical WSL gates (task13_wsl.sh: run_local+manifest+chunks+long_media+short_inference): 171 passed EXIT_CODE=0 at 4cfa7c0; 171 passed EXIT_CODE=0 at 86bd38f; 172 passed EXIT_CODE=0 at 6ef6c9b.
- PLAN DEVIATION: none.
- Residuals (non-blocking): `estimate()` pending-chunk count is None when the DAG manifest is absent (no plan to count against); `log_bytes` scans only top-level work_root logs; `batch()` re-runs `_ensure_dag_manifest` after a successful run (redundant reconcile I/O only).

### Task 13 review record (2026-09-22)

- reviewer: independent reviewer subagent (read-only; three-pass chain, fresh instances)
- review scope: 9e1d803..4cfa7c0 (feature).
- verdict (first pass): APPROVE_WITH_FIXES — Critical 0 / Important 1 / Minor 4:
  - F1 (Important): `_run_pipeline` ran `_ensure_dag_manifest` (which persists the DAG manifest when absent) before `_enforce_disk_gate`, so a first low-disk run could create `manifest.json` before the gate raised.
  - F2 (Minor): no test locked the gate threshold to `ceil((ffv1+png)*1.20)`.
  - F3 (Minor): disk-low test's qtgmc post-condition allowed a pre-canary placeholder.
  - F4 (Minor): CLI estimate test only checked exit 0 + `input_frame_count`.
  - F5 (Minor): measured test omitted the `predicted_tape_time.label` assertion.
- fix: 86bd38f (fresh fix agent, spec task13fix_spec.md): F1 gate-before-bootstrap + `_enforce_disk_gate` accepts a pre-loaded DAG dict or None + operator guide "any artifacts"; F2 margin-lock + boundary test; F3 tightened post-condition; F4/F5 test assertions.
- re-review (scope 4cfa7c0..86bd38f): APPROVE — Critical 0 / Important 0 / Minor 0; F1-F5 all RESOLVED with file:symbol evidence; no new findings. One residual: `batch()` still persisted the DAG manifest (pre-existing pattern) before the run-path gate — a deterministic window exists because `analyze`'s gate uses the weaker `ceil(ffv1*1.20)` threshold.
- follow-up fix: 6ef6c9b (spec task13fix2_spec.md): batch read-only DAG pre-load, canary decision from the loaded manifest, entry fields filled post-run; new test `test_tc37_batch_disk_gate_creates_no_dag_on_first_batch`.
- final delta review (scope 86bd38f..6ef6c9b): APPROVE — Critical 0 / Important 0 / Minor 0; the batch residual is CLOSED (read-only DAG pre-load, no manifest-persisting write before the run-path gate, entry fields absent on failed entries); TC-39 adjustment verified non-weakening; no new findings.
- TEST_EVIDENCE: Windows targeted 97 passed (6ef6c9b); canonical WSL gate 172 passed EXIT_CODE=0 (6ef6c9b).
- SCOPE_DEVIATIONS: none — 4cfa7c0 touches exactly the three in-scope files; 86bd38f the same three; 6ef6c9b two (docs untouched, no operator-guide wording change needed).
- PLAN DEVIATION: none.

### Task 14 GATE record (2026-09-22)

- GATE condition (plan Tasks 14/16): end-to-end long-run verification requires a real-device 29.97i (interlaced) VHS source.
- Evidence (coordinator, 2026-09-22; `ffprobe -show_entries stream=avg_frame_rate,r_frame_rate,field_order` on every local source in `E:/vhs-upscale/input/`):
  - `VHS_10s.mkv`: `progressive, 30/1, 30/1`
  - `VHS_10s_704x396.mkv`: `progressive, 30/1, 30/1`
  - `VHS_10s_ready.mkv`: `progressive, 30/1, 30/1`
  - `VHS_10s_720x405.mkv`: invalid MKV (`EBML header parsing failed`) — unusable as a source at all.
- Decision: GATE NOT met — no 29.97i source available locally. Per plan, Tasks 14 and 16 are recorded and stopped (no real-material 29.97i runs executed). Synthetic fixtures do not substitute for real-device verification (prohibited).
- Residual: the pipeline's interlaced handling remains covered by synthetic/fixture contracts (TC-33/TC-35 interlaced probes); a 29.97i source unblocks Tasks 14/16 without code changes expected.
- PLAN DEVIATION: none.

### Task 15 implementation record (2026-09-22)

- Task 15 (plan L929-944; test_cases.md section 10): fresh full-suite execution per marker groups + independent review of the whole branch.
- Marker groups executed (fresh, 2026-09-22):
  1. Windows unit (`-m "not vapoursynth and not gpu and not integration and not e2e"`, C:/Python313, fresh --basetemp): **244 passed, 1 skipped, 79 deselected, EXIT 0** (skip = test_asset_contract module, torch absent on this host via pytest.importorskip).
  2. Windows VapourSynth (`-m vapoursynth`, fresh --basetemp): 1 failed, 1 skipped, 322 deselected, EXIT 1. Failure: test_qtgmc_contract.py::test_tc01_vapoursynth_preflight_requests_first_frame - check_vapoursynth preflight exits 1 because VapourSynth/vsjetpack is not installed on this host (install_vapoursynth.ps1 -Apply requires user approval; handover 2026-09-20 documents -m vapoursynth as RED before -Apply). Environmental, not a regression.
  3. FFmpeg integration (`-m integration`, fresh --basetemp): 58 passed, 7 failed + 10 errors, 1 skipped, 248 deselected, EXIT 1. All failures/errors share one cause: module-scoped vhs_fixture ffprobe stream lacks color_primaries on this Windows host (PreflightError, no guessing). Same environmental failure mode recorded in handover 2026-09-21; the identical tests pass on the WSL canonical harness.
  4. WSL GPU (`-m gpu`, wsl.exe Ubuntu-22.04, venv-rocm python, /mnt/e path): **3 passed, 328 deselected, EXIT 0**.
  5. e2e canary (`-m e2e`): BLOCKED - no test carries the e2e marker (marker unregistered in pytest.ini); the 30-60s real-material canary (TC-40) is blocked by the Task 14 GATE (no real 29.97i VHS source on this machine; see task14_record.md and the Task 14 GATE record below).
- WSL canonical full suite (all 13 test files, venv-rocm python): **330 passed, 1 failed** - the single failure is test_qtgmc_contract.py::test_tc01_vapoursynth_preflight_requests_first_frame (same environmental VapourSynth absence as the Windows group; the VapourSynth environment lives on the Windows side). All unit/integration/gpu-relevant tests green on the canonical harness.
- Final independent review (scope fb97c608..d54404c, integration-level, read-only): **APPROVE_WITH_RESIDUALS**. Critical 0 / Important 2 / Minor 3.
  - F1 (Important, cross-task Task 10 x Task 11): the orchestrator tape_chunks stage read result["output_path"]/hash/mtime_ns/wall_time_seconds from the chunk runner, but the real worker pipeline/tape_chunk.py returns a ChunkResult dataclass (chunk_ffv1/sidecar/status/error/metrics/core_frames) - a guaranteed KeyError once the two modules are connected; per-task tests passed only because they injected fakes matching the dict shape. **FIXED** in commit fc7ce6e: _normalize_chunk_result in pipeline/run_local.py (Mapping pass-through; dataclass adaptation; fail-closed RunLocalError on status != "complete" or missing chunk_ffv1; wall time mapped from the worker's metrics["wall_seconds"]) + 2 regression tests using the real ChunkResult/ChunkRange shapes (RED confirmed pre-fix: KeyError swallowed -> chunk FAILED).
  - F2 (Important, cross-task Task 11/12): no default runner wired for references and tape_chunks stages - the CLI cannot execute those stages end-to-end. **WAIVED** with rationale: (1) the plan section 17 gates this branch at the Task 14 GATE (no real 29.97i source; no user preview selection), which is the plan's own stopping condition; (2) wiring the unattended TAPE stages requires the WSL ROCm subprocess environment plus real 29.97i material to verify end-to-end - the standing rule forbids substituting synthetic fixtures for real-device verification, so implementing it now would add unverifiable production code; (3) already documented as an accepted residual in the Task 12 review record. Re-entry for Task 16: wire default references + tape_chunks runners (the F1 adapter already makes chunk records safe for the real worker return type).
  - M1-M3 (Minor, accepted per-task residuals, verified non-compounding): sidecar-less chunk window between publish and sidecar write (pipeline/tape_chunk.py:509-514, recovery edge case per plan 7.6); config.feature_shard_frames absent from _DEPENDENTITY_TABLE (covered indirectly via the reference cache key signature); --global-start-frame value not compared to first frame name (format still enforced).
- Follow-up commits after the final review: d54404c (test collection guard, see note above) and fc7ce6e (F1 fix, see above). Post-fix verification: Windows test_run_local.py 35 passed (fresh basetemp); Windows unit group re-run 246 passed, 1 skipped, EXIT 0; WSL gate (run_local+tape_chunk+manifest+long_media+short_inference) 172 passed, EXIT 0.
- Review of fc7ce6e delta (fresh reviewer instance, read-only): **APPROVE_WITH_RESIDUALS** (confidence 0.93, overall correct). F1 verified complete and fail-closed: Mapping pass-through unchanged for injected runners; non-Mapping raises RunLocalError unless status == "complete" and chunk_ffv1 present; record fields derived from the real file; wall time mapped from the worker's metrics["wall_seconds"]; the tape_chunks branch in _run_pipeline is the only chunk-runner callsite (resume/batch/canary/run all funnel through it); no test weakened. Residual risks (not patch defects): (1) the CLI still passes no default chunk/references runner (the waived F2 scope; wiring also needs (manifest, index, request) -> TapeChunkRequest signature glue); (2) the pre-existing except-Exception in the tape_chunks branch swallows the RunLocalError detail, so a failed chunk record carries status FAILED without the worker error payload.

### Task 15 review record (2026-09-22)

- reviewer: independent reviewer subagent (read-only; two fresh instances)
- pass 1 (whole-branch integration, scope fb97c608..d54404c): APPROVE_WITH_RESIDUALS - Critical 0 / Important 2 / Minor 3. F1 FIXED in fc7ce6e; F2 explicitly waived with rationale (see implementation record); M1-M3 accepted, verified non-compounding.
- pass 2 (delta on d54404c + fc7ce6e): APPROVE_WITH_RESIDUALS (confidence 0.93, overall correct) - F1 verified complete and fail-closed; no new Critical/Important; residual risks: CLI default runner still unwired (F2 scope) and failed chunk record carries no worker error payload (pre-existing except-Exception behavior).
- TEST_EVIDENCE: Windows unit group 246 passed + 1 skipped EXIT 0 (fc7ce6e); WSL canonical gate (run_local+tape_chunk+manifest+long_media+short_inference) 172 passed EXIT 0 (fc7ce6e); WSL canonical full suite 330 passed / 1 failed (TC-01 VapourSynth preflight, environmental); WSL GPU group 3 passed EXIT 0.
- PLAN DEVIATION: none - Task 15 step 8 (resolve all Critical/Important) satisfied by fc7ce6e (F1) + documented waiver (F2); the e2e marker group is blocked by the Task 14 GATE (no 29.97i source; recorded in the Task 14 GATE record).
