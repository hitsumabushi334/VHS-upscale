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

