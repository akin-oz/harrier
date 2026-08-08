---
spec: 004
title: Tracker store schema and CSV migration
status: accepted
approved: yes
milestone: M1
depends: [002,003]
---

# Spec 004: Tracker store schema and CSV migration

## Problem

The tracker becomes SQLite as truth (ADR-003). The old CSVs must migrate with full fidelity, including the notes key=value data.

## Scope

- SQLite schema: jobs and contacts tables, status lifecycle constraint, unique dedupe indexes (url, company+title, external_key)
- the single write-path module harrier.tracker with transition enforcement
- one-shot migration from ~/job-hunt-local/tracker/jobs.csv and contacts.csv expanding notes key=value pairs into columns
- CSV export in the current 20-column and 17-column shapes (just export)

## Acceptance criteria

- [ ] migration asserts row counts (701 jobs, 146 contacts at inventory time) and field fidelity against the source
- [ ] an illegal status transition raises
- [ ] export then re-import round-trips

## Proof / origin

scripts/job_sources.py TRACKER_FIELDS; scripts/jobs.py:43; docs/adr/ADR-003-tracker-store.md

## Out of scope

To be refined before approval. This stub sequences the backlog; scope narrows or
splits when the spec is drafted for real.
