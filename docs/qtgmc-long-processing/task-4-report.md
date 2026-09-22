# Task 4 implementation report

## Files changed

- `requirements-rocm-inference.txt`
- `scripts/wsl/setup_rocm_env.sh`
- `scripts/check_rocm.py`
- `pytest.ini`
- `tests/test_rocm_scripts.py`
- `.superpowers/sdd/implementation_plan/task-4-report.md`

No other worktree files were edited. `progress.md` was not edited.

## RED evidence

The required dependency-free Bash contract was run before the setup script
existed:

```text
wsl.exe -- bash -lc 'set -u; root=/mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna; script="$root/scripts/wsl/setup_rocm_env.sh"; tmp=$(mktemp -d); cleanup() { rm -rf "$tmp"; }; trap cleanup EXIT HUP INT TERM; if [ ! -f "$script" ]; then printf "RED: setup script missing; normal dry-run contract cannot execute\n"; exit 1; fi; legacy=$(bash "$script" --dry-run --runtime legacy 2>&1); status=$?; printf "%s\n" "$legacy"; if [ "$status" -ne 0 ]; then printf "RED: normal legacy dry-run failed status=%s\n" "$status"; exit 1; fi; case "$legacy" in *runtime=legacy*replace_legacy_hsa=true*) : ;; *) printf "RED: legacy plan missing\n"; exit 1 ;; esac; rocdxg=$(bash "$script" --dry-run --runtime rocdxg 2>&1); status=$?; printf "%s\n" "$rocdxg"; if [ "$status" -ne 0 ]; then printf "RED: normal ROCDXG dry-run failed status=%s\n" "$status"; exit 1; fi; case "$rocdxg" in *runtime=rocdxg*replace_legacy_hsa=false*) : ;; *) printf "RED: ROCDXG plan missing\n"; exit 1 ;; esac; printf "GREEN: normal dry-action contract passed\n"'
```

Exact result summary:

```text
RED: setup script missing; normal dry-run contract cannot execute
exit_code=1
```

The planned WSL pytest command was also attempted before the test dependency
was available:

```text
wsl.exe -- bash -lc 'cd /mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna && python3 -m pytest tests/test_rocm_scripts.py -v'
/usr/bin/python3: No module named pytest
exit_code=1
```

The test-first preflight regression was then run before its implementation
fix:

```text
PYTHONPATH=/mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna/.task4-pytest-deps python3 -m pytest /mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna/tests/test_rocm_scripts.py -q -p no:cacheprovider --basetemp /tmp/task4-pytest-venv -k normal_legacy
F                                                                        [100%]
AssertionError: assert 'python3.10-venv' in ...
focused_pytest_exit_code=1
```

During the later test-first preflight regression, the new test failed against
the implementation with `ERROR: Python executable is not available: python3`.

## GREEN evidence

The dependency-free Bash contract exercised the normal legacy dry-action plan
with controlled Ubuntu 22.04, Python 3.10, and DXG fixtures, then exercised the
ROCDXG probe. It passed with:

```text
normal invocation: bash scripts/wsl/setup_rocm_env.sh --dry-run --runtime legacy --os-release /etc/os-release --python /usr/bin/python3 --dxg "$tmp/dxg" --venv "$tmp/.venv-rocm" --tmp-root "$tmp/temporary"
probe invocation: bash scripts/wsl/setup_rocm_env.sh --dry-run --runtime-probe rocdxg --tmp-root "$tmp/temporary"
supported_setup=legacy-rocm-7.2
runtime=legacy
replace_legacy_hsa=true
legacy_fallback=false
action=create_venv
plan=sudo apt-get install --yes python3.10-venv
torch_wheel=torch-2.9.1+rocm7.2.0...
hsa_source=/opt/rocm/lib/libhsa-runtime64.so.1.2
runtime=rocdxg
replace_legacy_hsa=false
legacy_fallback=false
GREEN: normal dry-action contract passed
exit_code=0
```

The focused fix verification passed:

```text
1 passed, 12 deselected in 0.09s
focused_pytest_exit_code=0
```

The full planned pytest contract was then run under WSL Python 3.10.12 with
pytest 8.3.5 and temporary test-only dependencies:

```text
PYTHONPATH=/mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna/.task4-pytest-deps python3 -m pytest /mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna/tests/test_rocm_scripts.py -q -p no:cacheprovider --basetemp /tmp/task4-pytest-final
.............                                                            [100%]
13 passed in 0.77s
pytest_exit_code=0
```

## Additional verification

- `bash -n scripts/wsl/setup_rocm_env.sh` and `python3 -m py_compile scripts/check_rocm.py` passed with exit code 0.
- The focused preflight regression passed after the fix: `1 passed, 12 deselected`.
- The check contract now emits valid schema JSON with `error.type=runtime_identity` when no explicit runtime or validated venv marker is supplied; an explicit-runtime run in the unconfigured WSL environment reports the Torch import failure in the same envelope.
- The tests cover explicit runtime selection, legacy 7.2 wheel planning, ROCDXG artifact fail-closed behavior, no legacy fallback/HSA replacement, conflicting runtime sources, existing venv refusal, dry-run non-mutation, temporary cleanup, Torch import/result-collection failure JSON, complete runtime/profile/OS/Python/DXG identity, signal-safe atomic JSON output, and separate FP16 exit code 2.
- No ROCm installation or Task 5 hardware setup was performed.

## Fix round

The reviewed pre-fix head was `884b648d611d56c4f8640d6e85c4c5ff0ebabc1b`.

### RED evidence

The new atomic JSON signal regression failed before its production fix because
the temporary file was left behind:

```text
& 'C:\Python313\python.exe' -m pytest tests/test_rocm_scripts.py -q -p no:cacheprovider --basetemp 'E:\vhs-upscale\task4-pytest-red-json-signal' -k removes_atomic_json_temp_file_on_sigterm
F                                                                        [100%]
1 failed, 23 deselected in 0.27s
AssertionError: assert not [WindowsPath('.../.check.json....tmp')]
```

The pre-fix WSL-backed run reached the expected missing-contract failures for
dry-run mutation, ROCDXG acceptance, Ubuntu ID validation, and runtime
ordering before the pre-fix command-failure cleanup path hung. The JSON
identity/SystemExit regressions were then run as focused tests; the atomic
JSON signal regression above provides the explicit RED evidence for that
round.

### GREEN evidence

Focused setup contracts passed after the Bash changes:

```text
& 'C:\Python313\python.exe' -m pytest tests/test_rocm_scripts.py -q -p no:cacheprovider --basetemp 'E:\vhs-upscale\task4-pytest-green-setup3' -k 'conflicting_runtime_sources or rocdxg_setup_fails_closed_without_approved_artifacts'
..                                                                       [100%]
2 passed, 21 deselected in 0.28s
```

Focused JSON contracts passed:

```text
& 'C:\Python313\python.exe' -m pytest tests/test_rocm_scripts.py -q -p no:cacheprovider --basetemp 'E:\vhs-upscale\task4-pytest-green-check3' -k check_
........                                                                 [100%]
8 passed, 15 deselected in 2.04s
```

The atomic JSON signal regression passed:

```text
& 'C:\Python313\python.exe' -m pytest tests/test_rocm_scripts.py -q -p no:cacheprovider --basetemp 'E:\vhs-upscale\task4-pytest-green-json-signal' -k removes_atomic_json_temp_file_on_sigterm
.                                                                        [100%]
1 passed, 23 deselected in 0.27s
```

### Final verification

The fresh full Task 4 suite passed:

```text
& 'C:\Python313\python.exe' -m pytest tests/test_rocm_scripts.py -q -p no:cacheprovider --basetemp 'E:\vhs-upscale\task4-pytest-final-3'
.............ss.........                                                 [100%]
22 passed, 2 skipped in 3.91s
```

The two skipped tests are the setup command-failure and SIGTERM delivery
tests, gated only when Windows drives WSL; the same behavioral tests remain
enabled for a native Linux/WSL Python run. The final checks also passed:

```text
wsl.exe -- bash -lc 'bash -n /mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna/scripts/wsl/setup_rocm_env.sh'
& 'C:\Python313\python.exe' -m py_compile scripts/check_rocm.py tests/test_rocm_scripts.py
git diff --check
```

No fix commit SHA is recorded in this report because the report is part of the
fix commit; recording that new SHA here would be self-referential.

## Second fix round

The reviewed head for this round was `b45b3c4338242b48247115b1dc68c12e8c09c7e9`.
The rereview findings were limited to setup child-signal forwarding, non-dry
ROCDXG fail-closed coverage, and test-environment sanitization.

### RED evidence

The new cleanup regressions were run through the actual Windows-to-WSL
launcher before changing the setup script. The command-failure path timed out
while the poisoned child remained in the foreground, and the signal path timed
out after only setup was signaled. This reproduced the missing active-child
forwarding behavior.

The focused pre-fix run reported:

```text
...FF                                                                    [100%]
3 passed, 2 failed, 20 deselected in 10.77s
```

The signal fixture was then corrected to protect WSL launcher variables from
the Windows command boundary; with production still unchanged, the signal
regression continued to fail by `TimeoutExpired` after 10 seconds. The
non-dry ROCDXG and ambient-environment tests passed against the existing
fail-closed implementation, establishing coverage for those review contracts.

### GREEN evidence

After `run_command` began tracking its active child, INT/TERM/HUP traps
forwarded the signal, waited for the child, and allowed the EXIT cleanup trap
to remove setup state. The focused second-round suite passed:

```text
.....                                                                    [100%]
5 passed, 20 deselected in 1.82s
```

The Windows launcher now creates poison and hanging commands inside WSL,
injects PATH there, signals only setup, and verifies the specific fake child
is absent from `/proc` after setup exits. Both non-dry ROCDXG tests poison all
setup mutation commands and assert that no poison marker is written and that
the temporary root and venv remain absent. The harness removes every
setup-related variable and corresponding `WSLENV` entry inherited from the
test process, while preserving values explicitly supplied by each test.

### Final verification

The fresh full Task 4 suite ran on the actual Windows-to-WSL path with no
skips:

```text
& 'C:\Python313\python.exe' -m pytest tests/test_rocm_scripts.py -q -p no:cacheprovider --basetemp 'E:\vhs-upscale\worktrees\tape-rocm-rx7900xt-luna\.task4-final-second3'
.........................                                                [100%]
25 passed in 5.96s
```

The final Bash syntax, Python compilation, and diff checks were also run after
the implementation changes. No ROCm installation or Task 5 execution was
performed, and no new commit SHA is recorded here because this report is part
of the commit being created.

## Residual risks

- The real ROCm userspace install, AMD wheel download, HSA runtime replacement,
  and RX 7900 XT tensor checks remain for Task 5 hardware setup.
- ROCDXG setup is intentionally disabled until an immutable approved
  ROCDXG-specific artifact allowlist exists; user-created manifests, markers,
  fake wheels, and renamed wheels cannot enable it or trigger legacy fallback.
- The current machine profile remains explicitly `legacy-rocm-7.2`; the
  `26.1.1` driver marker is not used or asserted on the current legacy path.

## Third fix round

The reviewed head for this round was `ec5f8f4b2e4cc834086ac1802ea462b7e04685b2`.
The final-review findings covered supervised child stdin and signal
dispositions, launch/reap PID races, venv rollback before the complete marker,
and direct setup-environment sanitization coverage.

### RED evidence

The new focused regressions were run through the actual Windows-to-WSL
launcher before the setup changes. The unchanged implementation reproduced
the missing launch-boundary option, empty child stdin, inherited ignored
signal dispositions, and venv retention after failure or signal:

```text
& 'C:\Python313\python.exe' -m pytest tests/test_rocm_scripts.py -q -p no:cacheprovider --tb=short -k 'signal or supervised_child or new_venv or sanitizes_every_setup_key'
.F..FFFF                                                                 [100%]
5 failed, 3 passed, 24 deselected
```

The INT signal case timed out while the direct background child remained
unsupervised; the parameterized TERM/HUP cases and sanitizer regression
provided the existing behavior baseline.

### GREEN evidence

The focused third-round regressions passed after adding the parent and child
disposition reset wrappers, explicit `spawning`/`running` state with queued
signals, owned child-group forwarding and waits, atomic marker publication,
and rollback ownership:

```text
& 'C:\Python313\python.exe' -m pytest tests/test_rocm_scripts.py -q -p no:cacheprovider --tb=short -k 'signal or supervised_child or new_venv or sanitizes_every_setup_key'
........                                                                 [100%]
8 passed, 24 deselected in 2.28s
```

The focused tests cover exact setup statuses 130/143/129, stdin delivery,
INT/TERM/HUP reset before child exec, deterministic signal delivery at the
launch boundary, owned-child cleanup, failure and signal rollback after venv
creation, retryability, and complete ambient `ROCM_*`/`TMPDIR` plus `WSLENV`
sanitization with explicit override retention.

### Final verification

The fresh full Task 4 suite ran on the Windows-to-WSL path with no skips:

```text
& 'C:\Python313\python.exe' -m pytest tests/test_rocm_scripts.py -q -p no:cacheprovider --tb=short
................................                                         [100%]
32 passed in 8.92s
```

The final checks also passed:

```text
wsl.exe -- bash -lc 'bash -n /mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna/scripts/wsl/setup_rocm_env.sh'
& 'C:\Python313\python.exe' -m py_compile scripts/check_rocm.py tests/test_rocm_scripts.py
git diff --check
```

No ROCm installation or Task 5 execution was performed. No new commit SHA is
recorded here because this report is part of the commit being created.

## Fifth fix round

The reviewed head for this round was `16bd130d93f3459119e1ce749aae0b8212c0f2a1`.
The remaining findings were explicit parent/supervisor readiness ordering and
replacement-safe rollback deletion.

### RED evidence

The deterministic regressions were added before changing production code and
failed against the reviewed head:

```text
FFFFF                                                                    [100%]
5 failed, 44 deselected in 1.06s
```

The three pre-readiness cases rejected the missing supervisor-stall contract,
and the directory and regular-file rollback replacement cases observed that
the replacement object had been removed.

### GREEN evidence

The focused readiness regressions passed after the supervisor and test fixture
changes:

```text
...                                                                      [100%]
3 passed, 46 deselected in 0.86s
```

The rollback replacement regressions passed for both a replacement directory
and a replacement regular file:

```text
..                                                                       [100%]
2 passed, 47 deselected in 0.91s
```

The supervisor now uses an explicit readiness FIFO. It announces readiness
only after establishing the process group, installing signal handlers, and
registering the command child; parent INT/TERM/HUP traps remain queued until
that announcement and forward the first signal once to the stable supervisor.
The supervisor forwards once to its owned process group and waits for every
group member to drain. The tests verify command-child and grandchild receipt,
exact statuses 130/143/129, exact-once delivery, launch-boundary ordering, and
full group cleanup.

Rollback now holds an open descriptor and device/inode/type identity for the
claimed venv. Cleanup revalidates ownership, atomically renames the current
path to a unique same-filesystem quarantine, verifies the quarantined object
and absence of `.rocm-runtime`, and deletes only that verified object. A
replacement between validation and quarantine is restored and preserved; the
tests cover both directory and inode/object replacement, alongside the prior
competing-creator and preexisting-venv contracts.

### Final verification

The fresh full Task 4 suite passed through the Windows-to-WSL launcher with no
skips:

```text
.................................................                        [100%]
49 passed in 11.25s
```

The final checks also passed:

```text
wsl.exe -- bash -n /mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna/scripts/wsl/setup_rocm_env.sh
& 'C:\Python313\python.exe' -m py_compile scripts/check_rocm.py tests/test_rocm_scripts.py
git diff --check
```

No review or progress file was edited. No ROCm installation or Task 5
execution was performed. No new commit SHA is recorded here because this
report is part of the commit being created.

## Fourth fix round

This round closes the remaining process-group ownership, venv rollback
ownership, dry-run/preflight, and sanitizer-test findings. No review or
progress file was edited.

### RED evidence

The focused Windows-to-WSL regression command was run against the unchanged
implementation after adding the new tests:

```text
& 'C:\Python313\python.exe' -m pytest tests/test_rocm_scripts.py -q -p no:cacheprovider --tb=short -k 'dry_run_fake_python or non_ubuntu_non_dry or competing_creator or replacement_venv or group_survives_leader_exit or signal_at_launch_boundary or signal_at_reap_boundary or harness_sanitizes_every_setup_key'
...FFFFF...FFF                                                           [100%]
8 failed, 6 passed, 30 deselected in 6.31s
```

The failures reproduced the accepted defects: both competing and replacement
venvs were deleted during rollback, the grandchild remained alive after its
leader exited, all launch-boundary signal cases failed to preserve the exact
status/cleanup contract, and the reap-boundary contract was not implemented.

### GREEN evidence

The focused regression suite passed after the setup changes:

```text
& 'C:\Python313\python.exe' -m pytest tests/test_rocm_scripts.py -q -p no:cacheprovider --tb=short -k 'dry_run_fake_python or non_ubuntu_non_dry or competing_creator or replacement_venv or group_survives_leader_exit or signal_at_launch_boundary or signal_at_reap_boundary or harness_sanitizes_every_setup_key'
..............                                                           [100%]
14 passed, 30 deselected in 2.50s
```

The setup command now launches a stable supervisor that owns the process
group, forwards queued and active INT/TERM/HUP signals to that group, and
keeps ownership until `/proc` confirms the group has drained. The tests cover
leader exit with a live grandchild, launch and reap boundaries for all three
signals, stdin delivery, reset signal dispositions, exact statuses 130/143/129,
and absence of every recorded group member.

Venv setup now atomically claims the previously absent path with `mkdir`,
records a device/inode/time/type ownership token, and rolls back only when the
token still matches and the complete `.rocm-runtime` marker is absent. The
competing-creator and replacement regressions verify that another directory is
never deleted. Dry-run Python accepts only `--version`; non-Ubuntu non-dry
preflight rejects before invoking any mutator, including the explicit Python
path; and direct sanitizer tests verify all explicit path conversions and the
exact final `WSLENV` tokens.

### Final verification

The fresh full Task 4 suite passed through the actual Windows-to-WSL launcher
with zero skips:

```text
& 'C:\Python313\python.exe' -m pytest tests/test_rocm_scripts.py -q -p no:cacheprovider --tb=short
............................................                             [100%]
44 passed in 9.34s
```

No ROCm installation or Task 5 execution was performed. No new commit SHA is
recorded here because this report is part of the commit being created.

## Recovery acceptance round

This recovery worker independently re-read the Task 4 brief, report, and both
acceptance reviews, then inspected the complete uncommitted production and
test diff. Only the setup script, its regressions, and this report were
changed; no review or progress file was edited. No ROCm installation or Task 5
execution was performed.

### RED evidence

The three new acceptance regressions were run against the pre-fix working
implementation before the production fixes were completed:

```text
& 'C:\Python313\python.exe' -m pytest tests/test_rocm_scripts.py -q -p no:cacheprovider --tb=short -k 'replacement_after_quarantine_verification or supervisor_startup_failure or readiness_channel_has_no_parent_fifo_writer or reap_boundary_cannot_rebind_a_reaped_job_identity'
F.FF
```

The failures reproduced the remaining quarantine replacement, parent FIFO
writer, and reaped-PID rebinding findings.

### GREEN evidence

The focused acceptance regressions passed after the fixes:

```text
....                                                                     [100%]
4 passed, 50 deselected in 2.74s
```

The adjacent rollback and signal-boundary contracts also passed:

```text
...............                                                          [100%]
15 passed, 39 deselected in 6.02s
```

Rollback now opens and verifies the claimed venv through a no-follow,
descriptor-relative handle, moves that verified object to a private cleanup
name before exposing the replacement test path, recursively removes through
the verified directory descriptor, and rechecks object identity before the
final removal. The WSL-mounted-filesystem fallback guards an existing private
target and leaves the quarantine when cleanup cannot prove identity. Readiness
now uses an atomic regular-file announcement with no parent writer; the parent
observes both READY and supervisor termination. Child identity includes start time and
process-group identity, and reaping clears the identity before the reap-boundary
signal hook runs.

### Final verification

The fresh complete Windows-to-WSL Task 4 suite passed with zero skips:

```text
......................................................                   [100%]
54 passed in 13.29s
```

The required final checks also passed:

```text
wsl.exe -- bash -n /mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna/scripts/wsl/setup_rocm_env.sh
& 'C:\Python313\python.exe' -m py_compile scripts/check_rocm.py tests/test_rocm_scripts.py
git diff --check
```

No new commit SHA is recorded here because this report is part of the commit
being created.

## Seventh fix round

The reviewed head for this round was `c8841f3dcaa9d21c8a3dcad3fce02ed4d8776271`.
The remaining acceptance findings were fallback pathname replacement during
quarantine detach/final removal, readiness-publication failure cleanup after
child spawn, and loss of the first queued signal on a no-readiness exit.

### RED evidence

The five new regressions were added before the production changes. The first
focused run against the unchanged script failed all five cases (`5 failed,
54 deselected`); the unchanged script rejected the new fault/race controls.
After only the test controls were wired, the final-removal case still failed
because the mounted-filesystem descriptor boundary prevented the injected
replacement from reaching the intended cleanup window. This established the
remaining cleanup boundary before the lifecycle implementation.

### GREEN evidence

The focused acceptance regressions passed after the fixes:

```text
.....                                                                    [100%]
5 passed, 54 deselected in 4.39s
```

The broader signal, supervisor, rollback, and cleanup slice also passed:

```text
............................                                             [100%]
28 passed, 31 deselected in 10.10s
```

Rollback now revalidates the actual generated fallback destination immediately
before detach, records fallback rename capability, closes the parent ownership
handle before detachment, and retains the verified quarantine when the mounted
filesystem cannot provide no-overwrite or object-bound final deletion. Atomic
cleanup also leaves the verified empty quarantine instead of pathname-removing
an object after its final identity check. The tests race an empty replacement
directory at the real generated destination during fallback rename and at the
last pre-removal boundary, and compare its device/inode identity before and
after setup exits.

The embedded supervisor now wraps startup, readiness publication, and wait in
`try/finally`; on pre-readiness failure it terminates its owned process group,
reaps the command child, and drains remaining live group members. The parent
has a bounded exception cleanup path for its owned supervisor group. A
post-spawn readiness fault with a queued INT, TERM, or HUP now drains the group
and returns exactly 130, 143, or 129 for the first queued signal.

### Final verification

The fresh complete Task 4 suite passed with zero skips:

```text
...........................................................              [100%]
59 passed in 14.75s
```

The required final checks also passed with exit code 0:

```text
wsl.exe -- bash -n /mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna/scripts/wsl/setup_rocm_env.sh
& 'C:\Python313\python.exe' -m py_compile scripts/check_rocm.py tests/test_rocm_scripts.py
git diff --check
```

No review or progress file was edited. No ROCm installation or Task 5
execution was performed. No new commit SHA is recorded here because this
report is part of the fix commit being created.

## Eighth fix round

The reviewed head for this round was `8a3e10b7de07445e592770e9d62257b8e1c69399`.
The remaining acceptance finding was the ordinary `os.rename()` fallback in
quarantine detach, which retained a check-then-rename replacement race.

### RED evidence

The final-syscall race regression and source contract were added before the
production fix and failed against the reviewed head:

```text
FF                                                                       [100%]
2 failed, 58 deselected
```

The staged-retention regression was then added before the staging lifecycle
change and failed because the incomplete final venv was still visible:

```text
F                                                                        [100%]
1 failed, 60 deselected
```

### GREEN evidence

The focused no-replace and source-contract regressions passed:

```text
..                                                                       [100%]
2 passed, 58 deselected
```

The affected staging, rollback, and race slice passed:

```text
.....                                                                    [100%]
5 passed, 56 deselected
```

The cleanup helper now requires `renameat2(RENAME_NOREPLACE)` and fails closed
when the primitive is missing, unsupported, or loses the destination race. No
ordinary rename fallback remains. Failed installs build in a unique staging
sibling; the requested venv is claimed with atomic no-replace `mkdir`, and the
complete runtime marker is copied only after the staged contents are ready.
Unsupported cleanup retains the owner-marked staging directory, while the
final-syscall regression proves the raced destination identity and owned source
remain intact.

### Final verification

The fresh complete Task 4 suite passed with zero skips:

```text
.............................................................            [100%]
61 passed in 17.54s
```

The required syntax, compile, and diff checks all exited 0:

```text
wsl.exe -- bash -n /mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna/scripts/wsl/setup_rocm_env.sh
& 'C:\Python313\python.exe' -m py_compile scripts/check_rocm.py tests/test_rocm_scripts.py
git diff --check
```

No review or progress file was edited. No ROCm installation or Task 5
execution was performed. No new commit SHA is recorded here because this
report is part of the fix commit being created.

## Ninth fix round

The reviewed head for this round was `8a691058ccd2c8cb83d9201f375ad53945ae7f2a`.
The acceptance findings were final publication through a pre-created and
copied destination, plus a quarantine test whose name and hook described a
later removal boundary that it did not reach.

### RED evidence

The new publication source contract failed before the implementation change
because the setup script still contained `copytree` and the destination
`mkdir`/copy path. The deterministic publication regression also found no
retained staging directory against the unchanged lifecycle. The interrupted
publication identification regression then failed before the cleanup message
was added.

### GREEN evidence

The focused publication suite passed:

```text
.......                                                                  [100%]
7 passed, 62 deselected in 7.74s
```

The adjacent rollback and quarantine suite passed:

```text
.................                                                        [100%]
17 passed, 52 deselected in 11.30s
```

Publication now builds and marks a unique sibling staging directory, then
calls one `renameat2` with `RENAME_NOREPLACE` against the absent final name.
The final directory is never pre-created and no tree copy is used. Unsupported
filesystems and destination races leave the final path absent or untouched and
retain the complete staging directory, with its path reported for manual
recovery or retry. Successful publication atomically removes the staging name
and removes the owner marker from the final venv. Publication failure,
interruption, retry, destination race, and successful no-staging behavior are
covered by deterministic tests. The obsolete quarantine case now names and
hooks the detach boundary; the final-syscall race remains covered separately.

### Final verification

The fresh complete Task 4 suite passed with zero skips:

```text
.....................................................................    [100%]
69 passed in 20.82s
```

The final checks also passed:

```text
wsl.exe -- bash -n /mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna/scripts/wsl/setup_rocm_env.sh
& 'C:\Python313\python.exe' -m py_compile scripts/check_rocm.py tests/test_rocm_scripts.py
git diff --check
```

No review or progress file was edited. No ROCm installation or Task 5
execution was performed. No new commit SHA is recorded here because this
report is part of the fix commit being created.

## Tenth fix round

The reviewed head for this round was `a97a6c4ee880f321b06afd6be7b835354c74e24d`.
Only the setup script, its regression tests, and this report were changed. No
review or progress file was edited, and no ROCm installation or Task 5 work was
performed.

### RED evidence

The new regressions were run before changing the setup script:

```text
FFF.F                                                                    [100%]
4 failed, 1 passed, 68 deselected in 3.78s
```

The direct retry left one retained staging directory after publishing a new
staging directory. The post-rename helper cases stopped at the missing test
boundary, and two complete owned retained stages were silently ignored while
a new setup succeeded.

### GREEN evidence

After the lifecycle changes, the focused regression slice passed:

```text
.....                                                                    [100%]
5 passed, 68 deselected in 3.41s
```

Publication now reconciles the final and staging paths against the held
directory descriptor/device/inode identity after every atomic helper return and
again during EXIT cleanup. A committed rename is recognized as a complete
published final even if the helper reports an error or the parent is
interrupted before helper return; the final is retained and the owner marker is
cleaned only through that verified identity. Complete retained staging is
validated for the exact runtime/profile marker and owner identity, then
resumed with the same no-replace rename. Unowned, incomplete, and conflicting
staging is never reused. The direct retry test no longer deletes staging.

The fresh full suite before the report update passed with no skips:

```text
........................................................................ [ 98%]
.                                                                        [100%]
73 passed in 23.39s
```

### Final verification

The final full suite passed with zero skips after the report update:

```text
........................................................................ [ 98%]
.                                                                        [100%]
73 passed in 23.13s
```

The required final checks also passed with exit code 0:

```text
wsl.exe -- bash -n /mnt/e/vhs-upscale/worktrees/tape-rocm-rx7900xt-luna/scripts/wsl/setup_rocm_env.sh
& 'C:\Python313\python.exe' -m py_compile scripts/check_rocm.py tests/test_rocm_scripts.py
git diff --check
```

No ROCm installation or Task 5 execution was performed. No commit SHA is
recorded here because this report is part of the commit being created.

## Eleventh fix round

The reviewed head for this round was `214bac03b94d42ea1c23f4886ccc79ed201399e1`.
Only the setup script, its regression tests, and this report were changed. No
review or progress file was edited, and no ROCm installation or Task 5 work
was performed.

The acceptance review identified two root causes: retained staging was
validated only against its own inode and runtime, so a stage could be reused
for another destination; and owner-marker removal validated through the held
directory descriptor but then unlinked through the mutable destination
pathname.

### RED evidence

The new focused regressions were run against the unchanged head before the
production changes:

```text
Pytest: 1 passed, 4 failed
```

Both cross-destination cases reused the retained sentinel, the
descriptor-relative unlink source contract was absent, and the deterministic
replacement test failed because the owner-marker boundary was not implemented.

### GREEN evidence

The focused acceptance slice passed after the production changes:

```text
Pytest: 4 passed
```

The affected staging/publication/rollback slice also passed:

```text
Pytest: 20 passed
```

Staging now uses a safe basename plus SHA-256 namespace derived from the
immutable target-parent device/inode identity and basename. Owner and complete
runtime markers carry both destination fields, and retained staging is
resumed only when the namespace, parent identity, basename, owner identity,
and complete marker all match. Owner-marker removal opens the marker with
`O_NOFOLLOW` and unlinks it relative to the held `venv_owner_fd`; the race
regression confirms a replacement target keeps its marker while only the
owned published object loses its owner marker.

### Final verification

The fresh complete Task 4 suite passed with the original 73 contracts
preserved and four new regression cases added:

```text
Pytest: 77 passed
```

The required syntax, compile, and diff checks were run after this report was
updated and passed with exit code 0. No ROCm installation or Task 5 execution
was performed.
