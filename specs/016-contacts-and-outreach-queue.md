---
spec: 016
title: Contacts, staged discovery, outreach queue
status: shipped
approved: yes
milestone: M4
depends: [004]
---

# Spec 016: Contacts, staged discovery, outreach queue

## Problem

The contacts store and the staged-approval discipline: discovery may
stage candidates, but nothing writes a contact without an approval step.
The old modules operate on tracker/contacts.csv and shell between
scripts; harrier's contacts already live in the database (spec 004).

## Scope

- Package harrier.outreach:
  - contacts: DB-backed contact operations on the contacts table with
    the applied_job_title vs person_title split. upsert_contact merges
    by normalized linkedin_url (or person_name) key, merging linked_jobs
    (a json list of {company, job_title, job_url}) and preferring
    non-empty new values; update, delete, and status updates by
    identifier; contacts_for_job matches direct fields and linked_jobs;
    best_contact orders by relevance rank (recruiter, hiring_manager,
    eng_manager, team_lead, team_member, founder_cto, other) then fit
    score; infer_relevance classifies person titles
  - state: the outreach state machine over applied rows:
    needs_contacts, ready, sent, follow_up_sent, replied, snoozed.
    refresh_outreach_fields derives outreach_status, next_outreach_action
    (find contacts, send first outreach after 3 business days from
    applied_date, wait until outreach window, send follow-up after 4
    business days from last_outreach_at, wait for reply), the primary
    next_action mirror, contacts_found, best contact fields, and
    outreach_priority (high without contacts, medium with, low after
    reply). sync_tracker_outreach persists the derived fields for every
    job; mark-sent (sent then follow_up_sent), mark-replied, and snooze
    act by job id; the due queue filters applied rows to actionable
    next_outreach_action values
  - discovery: staged Apify profile search. Search specs from the
    company and role (recruiter, talent acquisition, role-focused
    leadership terms, founder/CTO only for smaller companies);
    normalize_profile_result splits applied_job_title from person_title
    and scores fit (company match, hiring-side title, role keyword
    overlap, region overlap, generic-HR penalty); merge_ranked_contacts
    dedupes by profile keeping the best score and merging reasons;
    candidates stage to data/outreach/{slug}-candidates.json with
    review_status pending; approve copies a candidate into the contacts
    store and marks it approved; reject only marks it; the best-contact
    mode stops searching early once a strong match (relevance in the
    hiring set, fit at least 70) exists
  - hunter: Hunter.io domain search, email finder, and verifier over
    urllib with HUNTER_API_KEY (50 free credits/month; domain search is
    the best value at 1 credit for up to 10 emails)
  - backfill: guest poster backfill over non-rejected LinkedIn rows via
    the spec 009 guest helpers, STAGED like every other discovery path:
    posters land in the job's candidates artifact with review_status
    pending and enter contacts only through approval (stated change from
    the old direct-write backfill; the approval invariant has no
    exceptions); posters already in contacts are skipped; dry-run
    reports without writing anything
- CLI: harrier find-contacts --job-id N [--best-only], harrier contacts
  {list | approve | reject | set-best}, harrier outreach
  {sync | due | mark-sent | mark-replied | snooze}, harrier
  backfill-posters [--limit N] [--dry-run]
- Tracker store gains update_contact_fields and delete_contact (contacts
  stay ADR-003 tracker-store domain)

## Stated changes from the old code

- Contacts live in the database (spec 004's table), not
  tracker/contacts.csv; the csv legacy-alias handling (job_title,
  role_title, confidence) stays in the migration path only.
- Candidate artifacts land under data/outreach/ (never-in-git), not
  runtime/.
- The Hunter key comes from the environment only; the old mcp.json
  fallback is dropped (secrets belong in .env, never a committed file).
- Queue actions address jobs by id instead of csv row selectors.

## Acceptance criteria

- [x] Behavior pins ported from the old tests/test_find_contacts.py and
      test_outreach_lib.py: search specs, applied_job_title vs
      person_title split, hiring-side scoring over generic HR, profile
      dedupe by best fit score, missing-token failure, staging writes no
      contacts, best-contact early stop, approve copies to contacts,
      relevance inference, state machine transitions (needs_contacts,
      ready with window, wait for reply with business-day math), due
      filtering, and same-person merge across jobs with linked_jobs
      (services/api/tests/test_outreach.py, 21 tests)
- [x] Nothing writes a contact without an approval step: staging alone
      never touches the contacts table
      (test_find_contacts_stages_candidates_without_writing_contacts),
      and the poster backfill stages rather than writes
      (test_backfill_stages_posters_without_writing_contacts)
- [x] A snoozed job never surfaces as due through sync
      (test_snoozed_job_without_contacts_stays_snoozed)
- [x] Outreach sent transitions are legal only from ready or sent
      (test_mark_sent_rejects_illegal_transitions)
- [x] All gates green on PR (PR #13)

## Proof / origin

Old repo scripts/outreach_lib.py, find_contacts.py, hunter_lib.py,
outreach_queue.py, backfill_linkedin_posters_guest.py,
tests/test_find_contacts.py, tests/test_outreach_lib.py. Proving file:
services/api/tests/test_outreach.py. Honest limitations: the Hunter
functions are proven only for the missing-key failure path (live API
calls are not exercised in tests), and the CLI commands are thin shells
over the proven library functions without their own subprocess tests.

## Out of scope

Outreach message drafts (spec 017), Gmail reply watching (spec 018),
the daily digest (spec 019), and bulk find_contacts_bulk orchestration
(folded into the batch surface later if still needed).
