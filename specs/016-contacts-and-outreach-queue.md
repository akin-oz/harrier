---
spec: 016
title: Contacts, staged discovery, outreach queue
status: accepted
approved: yes
milestone: M4
depends: [004]
---

# Spec 016: Contacts, staged discovery, outreach queue

## Problem

The contacts store and the staged-approval discipline.

## Scope

- contacts in the tracker store (ADR-003) with linked_jobs and the applied_job_title vs person_title split
- Apify profile search staging to candidates files; approve/reject; early stop on strong match
- bulk search over applied rows; Hunter.io lookup; guest poster backfill
- outreach state machine: needs_contacts, ready, sent, follow_up_sent, replied, snoozed; queue actions

## Acceptance criteria

- [ ] behavior pins from tests/test_find_contacts.py and test_outreach_lib.py pass
- [ ] nothing writes a contact without an approval step

## Proof / origin

scripts/outreach_lib.py; scripts/find_contacts.py

## Out of scope

To be refined before approval. This stub sequences the backlog; scope narrows or
splits when the spec is drafted for real.
