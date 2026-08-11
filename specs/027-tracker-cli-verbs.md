---
spec: 027
title: Tracker CLI verb parity
status: in-progress
approved: yes
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
- `next`: what to do now, ordered, with a limit. Every active row,
  including applications waiting on a reply.
- `review`: counts across every status, then the queue of rows still
  awaiting a decision from you. `applied` and `interviewing` are decided,
  so they are counted but not queued.
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
- A numeric selector is the job id, not a row position. The old CLI indexed
  into the CSV by row number, which moved whenever a row above it was added
  or removed, so the same number meant a different job from one day to the
  next. Ids are stable and are what the API and the web app already show.
- This spec originally required `next` and the digest to share one ordering
  rule. That was wrong and is corrected here: they answer different
  questions. `next` asks what to do in the next ten minutes and ranks by
  how close a row is to going out; the digest asks what happened today and
  what is overdue. Forcing one rule would have broken parity with the old
  ranking for no gain.
- `reject` records a reason where the old one accepted free text; the
  column already exists (`rejection_reason`).
- No `--external-key` argument. This spec first required `add` to refuse a
  duplicate by external key, which overreached: the old `add` had no such
  argument, and an external key is what a source importer assigns, not
  something a person types. `find_duplicate` still checks it, so a manual
  add colliding with an imported row is caught by URL or by company and
  title, which are the reachable paths (review finding on PR #27).

## Acceptance criteria

- [x] every old verb exists: add, shortlist, applied, interviewing, reject,
      track, next, review, reevaluate, alongside the shipped tailor,
      evaluate and answers
- [x] a numeric selector, a unique substring, and a substring matching
      several rows behave as specified, the last aborting with the
      candidates listed and changing nothing
- [x] a selector matching nothing exits non-zero and changes nothing
- [x] `applied` seeds the outreach block and the follow-up date
- [x] `add` routes through the shared scoring path and refuses a duplicate
      by URL and by company and title
- [x] `next` orders by pipeline stage then score, closest-to-sending
      first, never shows a rejected row, and puts a row with no recorded
      arrival date behind dated ones
- [x] `review` queues only undecided rows while counting all of them
- [x] a malformed `--applied-date` or a non-positive `--limit` is refused
      by the parser rather than reaching the domain
- [ ] All gates green on PR

Proven by services/api/tests/test_tracker_cli.py, 26 tests. By criterion:

- the verbs: `test_each_verb_sets_its_status`,
  `test_applied_seeds_the_outreach_block_and_the_follow_up`,
  `test_reject_records_the_reason`, `test_reject_without_a_reason_still_works`
- the selector: `test_a_numeric_selector_is_the_job_id`,
  `test_a_unique_substring_matches_one_row`, `test_the_url_is_searchable_too`,
  `test_an_ambiguous_selector_aborts_and_lists_the_candidates`,
  `test_a_selector_matching_nothing_is_an_error`,
  `test_an_ambiguous_selector_changes_nothing`
- add: `test_add_routes_through_the_shared_scoring_path`,
  `test_add_refuses_a_duplicate_url`,
  `test_add_refuses_a_duplicate_company_and_title`,
  `test_add_without_a_company_is_refused`
- the queue: `test_rank_puts_the_nearest_to_sending_first`,
  `test_score_breaks_ties_within_a_stage`,
  `test_rejected_rows_never_appear_in_the_queue`,
  `test_an_undated_row_sorts_behind_a_dated_one`,
  `test_review_lists_only_rows_awaiting_a_decision`,
  `test_next_still_shows_decided_but_active_rows`
- the parser: `test_a_malformed_applied_date_is_refused_by_the_parser`,
  `test_a_bad_limit_is_refused_by_the_parser`
- reevaluate: `test_reevaluate_rescores_against_the_current_config`

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
