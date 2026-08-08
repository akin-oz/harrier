---
spec: 007
title: Screening core: shared shape, gates, scoring, dedupe
status: accepted
approved: yes
milestone: M2
depends: [004]
---

# Spec 007: Screening core: shared shape, gates, scoring, dedupe

## Problem

The shared screening path is the heart of discovery and must port with behavior pinned before any importer lands.

## Scope

- normalized job shape (all fields of make_normalized_job)
- gate order: seen-state, hold list, title rules, remote/EMEA policy, tracker dedupe, enrichment, scoring with cutoff 55
- EU-permit phrases as positive weights; linkedin_search region bypass; location-only negative hints
- archetype detection (single implementation)
- description cache and enrichment fetch; seen-state migration from the old repo

## Acceptance criteria

- [ ] behavior pins ported from tests/test_job_sources.py all pass
- [ ] the documented false-positive cases (Remote Home Office, US-offices-in-description) stay accepted

## Proof / origin

scripts/job_sources.py screen_jobs, score_job, remote_region_allowed

## Out of scope

To be refined before approval. This stub sequences the backlog; scope narrows or
splits when the spec is drafted for real.
