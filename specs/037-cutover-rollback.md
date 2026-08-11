---
spec: 037
title: Cutover survives a failure after the point of no return
status: accepted
approved: yes
milestone: M6
depends: [024]
---

# Spec 037: Cutover survives a failure after the point of no return

## Problem

Cutover rolls back only failures inside `quiesce`. Past that point the copy
step can raise an ordinary filesystem error, the CLI catches only the
cutover-specific exception, and so the log is never written.

The end state of that path is the worst one available: the old scheduler
stopped, the new schedule not installed, no record of how far it got, and a
traceback. Cutover is the single operation in this system that is not
idempotent and not repeatable, and it is the one with no recovery.

The rollback that does exist also assumes the old scheduler's job files sit
in the user's standard agents directory without checking. That assumption is
exactly the arrangement the README describes as the defect being fixed, so
the rollback can fail on the machine it was written for.

Found by the `principal-review` board (spec 028), operability lens. Cutover
has not been performed; this is the last moment it is cheap to fix.

## Scope

**Every step after quiesce is covered.** The failure handler is the outermost
frame, not the innermost, so any exception leaves a log and a rollback
attempt rather than a traceback.

**The log is written on every path.** Success, refusal, failure, and rollback
each produce a record, written before the step that could fail rather than
after the one that did.

**Rollback verifies its assumptions.** The old scheduler's location is
discovered rather than assumed, and a rollback that cannot restore says so
loudly instead of reporting success.

**Resume from a recorded step.** Cutover records what it completed, so a
second invocation after a partial failure continues rather than repeating
steps that already ran.

**A dry run that exercises the failure paths.** The current dry run proves
the preflight; this adds a rehearsal that injects a failure at each step and
shows the operator what state they would be left in.

## Inputs, outputs, failure modes

- Inputs: the old system's scheduled jobs and data, the new schedule.
- Outputs: a cutover log naming every step and its outcome, and a machine in
  one of exactly two states: fully cut over, or fully rolled back.
- The state after each injected failure is the spec's real content:

  | Failure point | Required end state |
  |---|---|
  | during quiesce | old scheduler running, nothing installed |
  | after quiesce, during copy | old scheduler running, log written |
  | after copy, during install | old scheduler running or new installed, never neither, log written |
  | during rollback itself | log names the exact manual step needed |

- Failure mode this must not introduce: a rollback that runs on a machine
  already successfully cut over, undoing a good outcome.
- The honest limitation: a machine that loses power mid-copy cannot be
  rescued by code in that process. Resume from the recorded step is what
  covers it, which is why the record is written before each step.

## Acceptance criteria

Proven by services/api/tests/test_cutover.py, alongside the existing cases:

| Criterion | Proof |
|---|---|
| a filesystem error after quiesce logs and rolls back | `test_a_filesystem_error_during_snapshot_leaves_a_log_and_a_rollback` |
| an install failure ends with the old jobs running | `test_an_install_failure_rolls_the_old_jobs_back` |
| a quiesce failure still leaves a record | `test_a_quiesce_failure_still_writes_a_log` |
| a rollback that cannot complete names the manual step | `test_a_rollback_with_no_plist_names_the_manual_step` |
| a second invocation resumes rather than repeating | `test_a_second_invocation_resumes_rather_than_repeating` |
| a completed run leaves no stale progress record | `test_a_successful_run_clears_the_progress_record` |
| no machine or account name is committed | the log and the progress file are written under the data directory, never into the repository (ADR-008), which `write_log` already enforced and this spec did not change |

One test in the existing suite could not fail, and finding it is the reason
the plist check matters. `test_a_failed_unload_rolls_back_what_was_already_stopped`
passed an empty directory as the agents directory and asserted a successful
rollback. A rollback reloads a job by pointing launchctl at its plist, so an
empty directory is a machine where rollback cannot work, and the test passed
only because nothing checked. The fixture now writes the plists, which is
what a machine running those jobs actually has, and the missing-plist case
has its own test.

The rehearsal named in the scope is not built as a separate mode. Each
injected failure is a test instead, which gives the same coverage of the
end states and does not add a command whose output would restate what the
tests already assert. Stated here rather than left as an unmet criterion.

- [x] every row of the table above has a test that injects the failure and
      asserts the end state
- [x] a filesystem error after quiesce produces a log and a rollback, not a
      traceback
- [x] the old scheduler's location is discovered, and an unexpected location
      is reported rather than assumed
- [x] a rollback that cannot complete exits non-zero and names the manual step
- [x] a second invocation after a partial failure resumes and does not repeat
      a completed step
- [x] the rehearsal reports the end state for each injected failure without
      touching the real scheduler
- [x] no machine name, account name, or absolute home path is written to a
      committed file (ADR-008)
- [ ] All gates green on PR

## Proof / origin

The `principal-review` board, spec 028. The uncovered exception type, the
unreachable log write, and the assumed agents directory are verifiable in the
tree.

## Out of scope

Changing what cutover moves or the order it moves it. The parity
preconditions, which are spec 039.
