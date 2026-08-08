---
spec: 008
title: ATS importers: Greenhouse, Ashby, Lever, RemoteOK
status: accepted
approved: yes
milestone: M2
depends: [007]
---

# Spec 008: ATS importers: Greenhouse, Ashby, Lever, RemoteOK

## Problem

The free sources, ingestion-only, with their hard-won fallbacks.

## Scope

- four source modules returning normalized jobs only
- Ashby HTML fallback on API 404; Lever EU API base; RemoteOK legal-stub skip
- feeds.txt netloc routing

## Acceptance criteria

- [ ] behavior pins from tests/test_feed_importers.py pass
- [ ] a source module importing scoring or tracker code fails the import-linter contract

## Proof / origin

scripts/import_greenhouse_jobs.py; import_ashby_jobs.py; import_lever_jobs.py; import_remoteok_jobs.py

## Out of scope

To be refined before approval. This stub sequences the backlog; scope narrows or
splits when the spec is drafted for real.
