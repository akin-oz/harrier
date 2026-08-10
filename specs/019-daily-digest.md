---
spec: 019
title: Daily digest
status: in-progress
approved: yes
milestone: M4
depends: [018, 016]
---

# Spec 019: Daily digest

## Problem

The Telegram summary tying together discovery, the outreach queue, and
the mail watch: one message per invocation, intended to run once a day
at 20:30 once spec 020 schedules it (this spec enforces no per-date
limit of its own).

## Scope

- harrier.digest with the five sections ported intact:
  1. New prospects today (added_at matches the target date, with the
     legacy auto_added/tier_a_seed notes fallback for migrated rows;
     first five listed)
  2. Top 3 prospects (highest fit score among prospect, shortlisted,
     tailored_cv_requested)
  3. Outreach actions due (applied rows grouped by next_outreach_action,
     excluding the wait states; five companies per group with an
     and-N-more line)
  4. Ghosted applications (applied 21+ days ago; shown only when
     non-empty, capped at ten with an and-N-more line)
  5. Updates needing action (the mail watch event log filtered to the
     digest's actionable kinds: interview_invite, scheduling_request,
     assessment, request_info, recruiter_reply; the target date only;
     deduplicated; newest first; five shown)
- The event source is spec 018's data/gmail-watch/events.jsonl, parsed
  tolerantly (the old HANDLER_OUTPUT prefix is accepted so a migrated
  legacy log still reads).
- run_digest(conn, target_date, dry_run, send) returns the rendered
  digest and the send result; dry-run prints and never sends.
- CLI: harrier digest [--date YYYY-MM-DD] [--dry-run]; the date
  override supports backfill.

## Inputs, outputs, failure modes

- Inputs: the tracker rows, the mail event log, the target date (UTC
  today by default), and the Telegram env. Outputs: one rendered digest
  string; one Telegram message unless dry-run.
- Failure modes: an invalid --date raises ValueError surfaced by the
  CLI; a missing event log yields an empty updates section; malformed
  event lines are skipped; a Telegram failure returns the send helper's
  code (0 sent, 1 failure, 2 missing configuration).

## Stated changes from the old code

- Rows come from the database; the shifted-column csv repair stays in
  the migration path only.
- The event source is the spec 018 event log under the data directory,
  not the repo-root gmail_handler.log.
- The run is a library function with an injectable send, so dry-run
  silence is pinned by a send that raises if called.

## Acceptance criteria

- [ ] The digest over fixtures renders all five sections
      (test_digest_renders_all_five_sections)
- [ ] Dry-run sends nothing (test_dry_run_sends_nothing: the send
      function raises if called)
- [ ] Ghosted uses the 21-day cutoff inclusively at the boundary, and
      the rendered label says at-least rather than more-than
      (test_ghosted_cutoff_boundary)
- [ ] A migrated row without added_at still lands in the new-prospects
      section via its auto_added or tier_a_seed note
      (test_legacy_auto_added_note_counts_as_added_at)
- [ ] A malformed event kind is skipped rather than aborting the digest
      (test_malformed_event_kind_is_skipped)
- [ ] Updates are date-filtered, deduplicated, and newest first
      (test_updates_filter_dedupe_and_order)
- [ ] All gates green on PR

## Proof / origin

Old repo scripts/send_daily_digest.py (no old tests existed; the pins
here are new). Proving file: services/api/tests/test_digest.py.

## Out of scope

Scheduling the digest at 20:30 (spec 020), and any additional digest
channels.
