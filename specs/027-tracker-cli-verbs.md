---
spec: 027
title: Tracker CLI verb parity
status: proposed
approved: no
milestone: M6
depends: [004, 013, 015]
---

# Spec 027: Tracker CLI verb parity

## Problem

harrier can find jobs and generate artifacts for them, and cannot move one
through the pipeline. There is no way to shortlist a prospect, mark a row
applied, reject one, or see what to do next. The store supports all of it
(`set_status`, `update_fields`, `add_job` shipped with spec 004); nothing
exposes it.

The old system's daily driver is `scripts/jobs.py` with thirteen verbs.
harrier has three of them: `tailor`, `evaluate`, `answers`. The other nine
were never specified. They fell between spec 004, which built the store and
defined the legal statuses, and specs 013 to 015, which built the artifact
commands and stopped there. `docs/parity-matrix.md` marks the row "keep, the
daily driver; the new CLI keeps verb parity", so this is owed work rather
than a change of mind.

This blocks the cutover outright. A system that cannot record that you
applied to something cannot replace the one that can.

## Scope

- Status transitions, one verb each, matching the old names:
  `shortlist`, `applied`, `interviewing`, `reject`, `track`.
- `add`: manual entry through the same scoring and dedupe path the capture
  endpoint uses, so a hand-added row is indistinguishable from a discovered
  one.
- `next`: what to do now, ordered, with a limit.
- `review`: the queue of undecided rows.
- `reevaluate`: rescore an existing row against current config.
- Selector semantics, shared by every verb that names a row: a numeric id,
  or a substring that must match exactly one row. Ambiguity aborts and
  lists the candidates rather than guessing. This is its own module and its
  own tests, because every mutating verb depends on it and the failure mode
  is silently editing the wrong job.
- `applied` keeps its side effects from the old system: it seeds the
  outreach block and the follow-up date (spec 016's fields).

## Inputs, outputs, failure modes

- Inputs: a selector, and for `add` the posting fields.
- Outputs: the mutated row, echoed back so the change is visible.
- Failure modes: an ambiguous selector aborts naming every candidate; a
  selector matching nothing exits non-zero; an illegal status transition is
  refused by the store; `add` on a duplicate URL or external key reports the
  existing row rather than creating a second.

## Stated changes from the old code

- The old CLI sets any status from any status, and spec 004 chose to keep
  that parity verbatim. These verbs inherit it: no transition graph is
  introduced here.
- `reject` records a reason where the old one accepted free text; the
  column already exists (`rejection_reason`).

## Acceptance criteria

- [ ] every old verb exists: add, shortlist, applied, interviewing, reject,
      track, next, review, reevaluate, alongside the shipped tailor,
      evaluate and answers
- [ ] a numeric selector, a unique substring, and a substring matching
      several rows behave as specified, the last aborting with the
      candidates listed and changing nothing
- [ ] a selector matching nothing exits non-zero and changes nothing
- [ ] `applied` seeds the outreach block and the follow-up date
- [ ] `add` routes through the shared scoring path and refuses a duplicate
      by URL and by external key, naming the existing row
- [ ] `next` and `review` order by the same rule the digest uses, so the
      two never disagree about what is due
- [ ] All gates green on PR

Proving symbols are named at implementation, in
services/api/tests/test_tracker_cli.py.

## Proof / origin

`docs/parity-matrix.md` rows "Tracker CLI verbs", "Selector semantics" and
"Manual add with dedupe and scoring", all marked keep. Origin:
`scripts/jobs.py` in the old repo. Found by a feature audit on 2026-08-11,
not by the parity checklist, which lists the row but had no ticked or
waived state to contradict.

## Out of scope

A transition graph or workflow validation, which would be a change rather
than parity. The GUI surfaces for the same actions (their own specs). The
vacancy liveness check, now recorded in the matrix and unbuilt (its own
spec, and closer to spec 025 in shape). Bulk contact search over applied
rows, also unbuilt and its own spec.
