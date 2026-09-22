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
