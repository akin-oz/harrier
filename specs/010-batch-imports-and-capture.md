---
spec: 010
title: Batch imports, capture endpoints, bookmarklets
status: accepted
approved: yes
milestone: M2
depends: [007]
---

# Spec 010: Batch imports, capture endpoints, bookmarklets

## Problem

Wellfound and WTTJ export ingestion plus the browser capture path.

## Scope

- wellfound and wttj export importers (JSON/CSV)
- capture endpoints on the API: GET/POST /capture/add with the 200/400/409/500 contract and mixed-content-safe HTML response
- bookmarklets doc updated for the new port

## Acceptance criteria

- [ ] behavior pins from tests/test_job_server.py pass against the new endpoints
- [ ] a captured job dedupes against the tracker (409)

## Proof / origin

scripts/job_server.py; docs/bookmarklets.md (old repo)

## Out of scope

To be refined before approval. This stub sequences the backlog; scope narrows or
splits when the spec is drafted for real.
