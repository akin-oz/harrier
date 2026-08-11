---
spec: 029
title: A failed run must not look like a quiet one
status: accepted
approved: yes
milestone: M6
depends: [011, 019]
---

# Spec 029: A failed run must not look like a quiet one

## Problem

This project exists because a scheduled job in the old system failed silently
for two months. The rewrite reproduces the mechanism.

`harrier discover` ends with a literal `return 0`
(services/api/src/harrier_cli/main.py). Every upstream failure is absorbed
into the summary rather than the exit status: per-source exceptions are caught
into a dict (services/api/src/harrier/discovery.py, the RemoteOK and Apify
branches), and `board_errors` is collected in
services/api/src/harrier/sources/__init__.py and read by nobody. The only
notification is gated on `totals["new_prospects"]` being non-zero
(services/api/src/harrier/discovery.py), so a run in which every board answers
404 and the Apify token is revoked exits 0, sends nothing, and is recorded by
launchd as a success.

The digest cannot correct it, because the digest has the same defect: it
renders `New prospects today: 0` identically whether discovery ran four times
today or last succeeded in March. No job records that it succeeded, no job
reads whether another one did.

Logging cannot correct it either. No module calls `basicConfig` or
`dictConfig`, so every `logger.info` is discarded and every `logger.warning`
goes out through `lastResort` with no timestamp and no level, appended
forever to an unrotated file. The Apify cost-gate skip is logged at INFO,
which means a skipped paid source is indistinguishable from demo mode and
from a crash.

Each of the three is survivable alone. Together they mean that no scheduled
job in this system can report that it is not working, which is the exact
property whose absence caused the outage the project was built in reaction
to.

Found by the `principal-review` board (spec 028) as its first-change
recommendation. The board rated three findings above this one on severity and
still put it first, on the grounds that every other fix is a change to
behaviour that currently reports nothing about whether it worked.

## Scope

**Exit status.** `harrier discover` exits non-zero when every attempted
source failed, and when zero sources were attempted. A run where some sources
succeeded and others failed exits zero, because that is a normal day: the
failures are reported, not fatal. `board_errors` and per-source `errors`
count toward the decision.

**Notification.** The Telegram summary is sent whenever notifications are on
and the run is not a dry run, rather than only when new prospects were found.
A run that found nothing is exactly the run the operator needs to hear about,
because it is the shape both a quiet week and a total outage take.

**Last success.** Each scheduled job records the time it last completed
successfully, in the database rather than in a file, so it survives the same
way the tracker does. Recorded on the success path only.

**The digest reports the schedule.** The digest leads with the age of each
scheduled job's last success. "Discovery last succeeded 61 days ago" cannot
be misread; `New prospects today: 0` is what a two-month outage currently
looks like.

**Logging.** One logging configuration, applied at every entry point: level,
timestamp, module, and a bounded file. The cost-gate skip and every other
decision that changes what a run did move to a level that is emitted.

## Inputs, outputs, failure modes

- Inputs: the run summary already assembled by `run_discovery`, and a new
  `job_runs` table holding one row per scheduled job name.
- Outputs: a process exit status, a Telegram message, a digest section, and
  log lines that carry a timestamp.
- The exit-status contract, so nothing is decided by accident:

  | Run | Exit |
  |---|---|
  | at least one source produced results | 0 |
  | at least one source succeeded, others failed | 0, failures reported |
  | every attempted source failed | non-zero |
  | zero sources attempted (nothing configured, all skipped) | non-zero |
  | dry run, any of the above | same as the real run |

  A source that is deliberately skipped (the Apify cost gate, demo mode) is
  not an attempt and not a failure. It is reported as a skip, with the reason,
  because a paid source silently not running is the case that motivated this.

- Failure mode this must not introduce: an exit status that fails a run the
  operator would call fine. Partial failure stays zero. The threshold is
  total failure, which is the only shape that is never normal.
- Failure mode this cannot fix: a job that hangs rather than fails. A hung
  Gmail poll holds its launchd label forever and never reaches any exit path
  (raised separately by the board). The last-success age is what surfaces it,
  which is why the age is reported rather than the last exit status.

## Acceptance criteria

Proving symbols are named at implementation, in
services/api/tests/test_run_outcome.py. They are not listed here, because
this spec is not built and naming symbols that do not exist is how a spec
starts lying (spec 025 records the same rule, after the mistake on PR #19).

- [ ] every row of the exit-status table has a test, including the two that
      must stay zero
- [ ] a run in which every board 404s and every source raises exits non-zero
      and sends a notification saying so
- [ ] the notification is sent when zero new prospects were found
- [ ] a dry run still sends nothing and writes nothing, including no
      last-success row
- [ ] each scheduled job writes a last-success timestamp only on success, and
      a failing run leaves the previous value untouched
- [ ] the digest names every scheduled job and the age of its last success,
      including a job that has never succeeded
- [ ] logging is configured once, emits a timestamp and a level, and the
      cost-gate skip appears in it
- [ ] no personal data enters the log configuration, the `job_runs` table, or
      the digest schedule section: job names and timestamps only (ADR-008)
- [ ] All gates green on PR

## Proof / origin

The `principal-review` board, convened under spec 028, five read-only
reviewers. This was its merged first-change recommendation, and the operability
lens traced it to the same failure the project was built in reaction to.

The three mechanisms are separately verifiable in the tree today: the literal
`return 0`, the truthiness gate on the notification, and the absence of any
`basicConfig` or `dictConfig` call in the package.

## Out of scope

Everything else the board raised. In particular the backup defect, the
screening rejections that cannot be reconsidered, and the truth gate that
omits rather than refuses are each larger than this and each need their own
spec.

Timeouts on the Gmail poll, and any change to `harrier schedule status`
beyond reading the new last-success rows.

Alerting on anything other than Telegram. Retry, backoff, or any change to
what a run does; this spec changes only what a run reports.
