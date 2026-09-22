# Task 14 GATE record (2026-09-21)

## Verdict: GATE NOT MET — record and stop (no material)

Task 14 (30-60s canary, TC-40) requires a real 29.97i VHS source. No such
material has been loaded into this machine.

## Evidence

- `E:/vhs-upscale/work/tape/` does not exist (no published long-form output,
  no source staging area).
- Workspace glob for `*.mkv` finds only synthetic pytest fixtures under
  `work/tmp/**` (basetemp trees); no 29.97i/interlaced source asset exists.
- Handover 2026-09-21 section 7: "no actual material has been loaded"
  (no real material loaded).
- All prior task evidence (Tasks 0-11) was produced with synthetic fixtures
  only, per the standing rule: never substitute synthetic fixtures for real
  device verification.

## Consequences

- TC-40 (30-60s end-to-end canary on real material) cannot be executed.
- Task 15 step 5 (fresh canary e2e) is blocked by the same missing material.
- Task 16 (full VHS run, TC-41) is blocked behind Task 15.
- Per the plan's stopping conditions, the worktree is brought to a
  mergeable state with Tasks 1-13 complete and the GATE recorded; it is NOT
  papered over with synthetic evidence.

## Re-entry conditions (for a future session)

1. Real 29.97i VHS source loaded, with field order auto-determined or
   user-overridden and preview selection valid (Tasks 6-7 gates).
2. 704x396-class or new-resolution GPU smoke PASS (Task 0/5 evidence exists
   for 704x396 synthetic; a real-material GPU smoke is still required).
3. Then execute Task 14 steps 1-6 (incl. fault-injected second chunk, resume,
   human visual seam check) and record TC-40 evidence in docs.
