---
spec: 009
title: Apify LinkedIn importer
status: accepted
approved: yes
milestone: M2
depends: [007]
---

# Spec 009: Apify LinkedIn importer

## Problem

The only paid source: run lifecycle, dataset-file mode, and the cache-everything cost saver.

## Scope

- actor start/poll/fetch with timeout; dataset-file ingestion mode
- description caching for all fetched items including rejected ones
- guest JD endpoint enrichment and poster extraction, staged into the outreach path (not direct contact writes)

## Acceptance criteria

- [ ] behavior pins from tests/test_import_apify_linkedin_jobs.py pass
- [ ] poster contacts land as staged candidates, not contacts rows

## Proof / origin

scripts/import_apify_linkedin_jobs.py link_publisher_contacts

## Out of scope

To be refined before approval. This stub sequences the backlog; scope narrows or
splits when the spec is drafted for real.
