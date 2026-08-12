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
each produce a record. The timing is after, not before: `_finish` in
`services/api/src/harrier/cutover.py` writes the log once the run has
resolved, and `_record_progress` records a step once it has completed, so
what is written is what happened rather than what was about to be attempted.
The honest limitation is that a process killed mid-step leaves the progress
record naming the last completed step and no final log at all; the progress
record is what makes the next invocation resumable, and an unreadable one
stops rather than restarting.

**Rollback verifies its assumptions.** The old scheduler's location is
discovered rather than assumed, and a rollback that cannot restore says so
loudly instead of reporting success.

**Resume from a recorded step.** Cutover records what it completed, so a
second invocation after a partial failure continues rather than repeating
steps that already ran.

**Failure paths that are exercised.** The current dry run proves the
preflight only: `services/api/tests/test_cutover.py::test_a_dry_run_touches_nothing`
asserts it writes nothing and unloads nothing, and
`::test_a_blocked_dry_run_reports_the_blockers_and_is_not_ok` asserts it
reports the blockers, which together are the whole of what it covers. Every row of the end-state table below is covered by a test
that injects that failure and asserts the state it leaves behind.

A separate operator-facing rehearsal mode is **out of scope**. It was in an
earlier draft of this spec and is not built: the tests give the same coverage
of the end states, and a command whose output restated what they already
assert would be a second description of the same behaviour to keep in step.
Recorded here rather than left as a criterion nothing satisfies.

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
  | during rollback itself | incomplete rollback: log names the exact manual step needed |

- A third end state exists and is deliberate: **incomplete rollback**. If the
  rollback itself cannot finish, the machine is neither cut over nor restored,
  and the log names the manual step that would finish it
  (`services/api/tests/test_cutover.py::test_a_rollback_with_no_plist_names_the_manual_step`).
  Pretending only two states exist would mean the one that needs a human is
  the one nobody wrote down.
- Failure mode this must not introduce: a rollback that runs on a machine
  already successfully cut over, undoing a good outcome.
- The honest limitation: a machine that loses power mid-copy cannot be
  rescued by code in that process. Resume from the recorded step is what
  covers it, which is why each completed step is recorded as it completes,
  through an atomic replacement so a half-written record cannot be mistaken
  for an empty one.

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
| the log and the progress record are written under the data directory, never into the repository | `services/api/tests/test_cutover.py::test_a_full_execution_quiesces_snapshots_verifies_and_installs` asserts the log path resolves under `data/`. Limitation: this proves where the files are written, not that nobody can commit them. Nothing under `data/` is tracked and it is gitignored, but that is a repository property enforced by `.gitignore` and the secret scan, not by this code (ADR-008) |

One test in the existing suite could not fail, and finding it is the reason
the plist check matters. `test_a_failed_unload_rolls_back_what_was_already_stopped`
passed an empty directory as the agents directory and asserted a successful
rollback. A rollback reloads a job by pointing launchctl at its plist, so an
empty directory is a machine where rollback cannot work, and the test passed
only because nothing checked. The fixture now writes the plists, which is
what a machine running those jobs actually has, and the missing-plist case
has its own test.

The rehearsal is out of scope rather than pending: see the scope section. It
was previously described here as an unbuilt scope item while the criteria
still ticked it, which is the shape of dishonest gate this repository exists
to avoid (review finding on PR #37).

- [x] every row of the table above has a test that injects the failure and
      asserts the end state
- [x] a filesystem error after quiesce produces a log and a rollback, not a
      traceback
- [x] the old scheduler's location is discovered, and an unexpected location
      is reported rather than assumed
- [x] a rollback that cannot complete exits non-zero and names the manual step
- [x] a second invocation after a partial failure resumes and does not repeat
      a completed step
- [x] every injected failure asserts its end state without touching the real
      scheduler (a separate rehearsal command is out of scope, above)
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
