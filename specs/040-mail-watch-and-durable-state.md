---
spec: 040
title: A hung poll, and state files that survive a crash
status: accepted
approved: yes
milestone: M6
depends: [017, 029]
---

# Spec 040: A hung poll, and state files that survive a crash

## Problem

The mail watch is scheduled on a fixed interval, and the scheduler will not
start a second instance of a job whose previous instance is still alive. The
mail client calls set no timeout. One socket that never returns therefore
stops the mail watch permanently while the scheduler continues to report the
job as running.

The schedule status command cannot surface it either: it prints its table and
returns zero regardless of a missing, unloaded, drifted, or failing job, and
its next-run column is arithmetic over the configuration file rather than
anything observed.

Two state files compound the failure mode. Both the mail watch state and the
screening seen state are written with a plain whole-file write, so a crash
mid-write truncates them. A truncated seen file is swallowed and read as an
empty set, which means the next run treats every posting as new and can fire
a burst of duplicate notifications.

And the mail watch writes an events file that accumulates third-party email
metadata and the first sentence of message bodies, unredacted and unrotated.
That is other people's correspondence in a growing file with no bound.

Found by the `principal-review` board (spec 028), operability lens.

## Scope

**Every network call has a timeout.** A poll that cannot finish fails, so the
next scheduled invocation runs. Failing repeatedly is visible through the
last-success age from spec 029; hanging once is not visible at all.

**A watchdog on the job itself**, so a poll that hangs below the socket layer
still ends. The interval is the bound: a job that has not finished by the time
its successor is due has already failed.

**Schedule status can fail.** It exits non-zero when a job is missing,
unloaded, drifted from the generated definition, or has not succeeded within
a multiple of its interval, and it reports observed last-success rather than
arithmetic.

**Atomic state writes.** Write to a temporary file in the same directory and
rename. A rename is atomic, so a crash leaves either the old file or the new
one, never half of either.

**A truncated state file is an error, not an empty set.** Reading zero
entries where a file exists is refused rather than treated as a fresh start,
because the fresh-start reading is what produces the notification burst.

**The events file is bounded and redacted.** Sender addresses and body
excerpts are the personal data of people who are not the user (ADR-008), so
what is retained is the minimum the feature needs, rotated, with the rest
dropped rather than accumulated.

## Inputs, outputs, failure modes

- Inputs: the mail provider, the schedule definitions, the two state files.
- Outputs: a poll that always terminates, a status command that can say no,
  and state files that are either valid or reported as damaged.
- Failure mode this must not introduce: a timeout so short that a slow but
  healthy mailbox is treated as failing, which would train the operator to
  ignore the signal. The timeout is derived from the interval, so it cannot
  be set to a value the schedule contradicts.
- Failure mode this must not introduce: dropping a message because a poll was
  cut short. The state advances only for what was fully processed.
- The interaction with spec 029 is deliberate: that spec makes a failed run
  visible, and this one makes a hung run into a failed run. Neither is
  sufficient alone.

## Acceptance criteria

Proven by services/api/tests/test_mail_watch.py:

| Criterion | Proof |
|---|---|
| a crash between write and rename leaves the previous file | `test_a_failed_write_leaves_the_previous_state_intact`, `test_no_temporary_file_is_left_behind` |
| a truncated file is damaged, not empty | `test_a_truncated_state_file_is_refused`, `test_an_empty_state_file_is_refused`, `test_a_state_file_of_the_wrong_shape_is_refused`, and `test_a_corrupt_state_file_is_refused_rather_than_read_as_empty` for the seen state |
| an absent file is still an ordinary first run | `test_an_absent_state_file_is_not_damage` |
| the events file is bounded and redacted | `test_the_archive_keeps_no_subject_or_body`, `test_the_archive_keeps_the_sender_domain_only`, `test_the_archived_fields_are_the_only_fields`, `test_the_archive_is_bounded` |
| schedule status exits non-zero per case | `test_an_unhealthy_job_reports_why` (four cases), `test_a_healthy_job_reports_no_problem`, `test_a_zero_exit_status_is_not_a_problem` |

## What is not built

The three timeout criteria above are unchecked, and that is the honest state
rather than an oversight to be discovered later.

The hung poll is the finding this spec was written for, and bounding it needs
a timeout threaded into the Gmail client's transport, plus a watchdog around
the poll itself. That is a change to how the mail client is constructed and
it wants its own spec, because the wrong shape of it either leaves the socket
unbounded anyway or cuts a healthy poll short and trains the operator to
ignore the signal.

What did land is everything the hang leaves behind: the state files it
corrupts, and the status command that could not report it. A hung poll is
still invisible; a crashed one no longer destroys the record, and a job that
stopped being loaded now fails `harrier schedule status` instead of printing
a line and returning zero.

Two things changed from what the spec assumed. Damage now **raises** rather
than logging, which supersedes the warn-and-continue that spec 031 shipped as
an interim and said belonged here: one failure now has one behaviour across
both state files. And the events file is redacted at the point of writing
rather than rotated with its contents intact, because the third-party subject
lines and body sentences are not something the feature needs at all, so
bounding how many of them are kept would have been the smaller half of the
fix.

- [ ] every outbound call in the mail path has a timeout, asserted by a test
      that fails if a new call site omits one
- [ ] a poll that exceeds its budget terminates and reports failure
- [ ] a partially processed poll advances state only for completed messages
- [x] schedule status exits non-zero for a missing, unloaded, drifted, or
      stale job, with one test per case
- [x] a crash between write and rename leaves the previous state file intact
- [x] a truncated state file is reported as damaged and does not read as
      empty, and no notification burst follows
- [x] the events file is rotated, bounded, and contains no third-party body
      text or address beyond what the feature requires (ADR-008)
- [ ] All gates green on PR

## Proof / origin

The `principal-review` board, spec 028. The absent timeouts, the
unconditional zero exit from schedule status, the two whole-file writes, and
the unbounded events file are verifiable in the tree.

## Out of scope

Changing mail providers or the authentication flow. What the watch does with
a message once it has one.
