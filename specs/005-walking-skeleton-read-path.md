---
spec: 005
title: Walking skeleton: tracker read path end to end
status: accepted
approved: yes
milestone: M1
depends: [004]
---

# Spec 005: Walking skeleton: tracker read path end to end

## Problem

Every risky seam must be crossed once before feature work: FastAPI route, exported OpenAPI, generated TS client, GUI list.

## Scope

- GET /jobs with filters (status, source) from the tracker store
- deterministic OpenAPI export script and committed packages/contract artifacts (ADR-005)
- openapi-typescript generation wired into just contract
- apps/web tracker list page consuming only the generated client (FSD layers in place)

## Acceptance criteria

- [ ] contract-drift CI job fails when a route changes without regeneration
- [ ] the GUI lists the migrated real rows locally and fixture rows in demo mode
- [ ] an invented field access in apps/web fails tsc

## Proof / origin

docs/adr/ADR-005-api-contract-seam.md

## Out of scope

To be refined before approval. This stub sequences the backlog; scope narrows or
splits when the spec is drafted for real.
