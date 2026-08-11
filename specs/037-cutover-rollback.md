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

Proving symbols are named at implementation, in
services/api/tests/test_cutover_failure.py.

- [ ] every row of the table above has a test that injects the failure and
      asserts the end state
- [ ] a filesystem error after quiesce produces a log and a rollback, not a
      traceback
- [ ] the old scheduler's location is discovered, and an unexpected location
      is reported rather than assumed
- [ ] a rollback that cannot complete exits non-zero and names the manual step
- [ ] a second invocation after a partial failure resumes and does not repeat
      a completed step
- [ ] the rehearsal reports the end state for each injected failure without
      touching the real scheduler
- [ ] no machine name, account name, or absolute home path is written to a
      committed file (ADR-008)
- [ ] All gates green on PR

## Proof / origin

The `principal-review` board, spec 028. The uncovered exception type, the
unreachable log write, and the assumed agents directory are verifiable in the
tree.

## Out of scope

Changing what cutover moves or the order it moves it. The parity
preconditions, which are spec 039.
