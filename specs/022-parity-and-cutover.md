---
spec: 022
title: Parity verification and cutover
status: accepted
approved: yes
milestone: M5
depends: [020,021]
---

# Spec 022: Parity verification and cutover

## Problem

Both systems run side by side until verified parity; cutover is an event.

## Scope

- parity checklist generated from docs/parity-matrix.md (every keep and change row)
- dual-run period: old launchd keeps writing old tracker; harrier runs scheduled dry-runs; daily diff of screening decisions
- final data migration refresh, launchd switchover, old repo archived read-only

## Acceptance criteria

- [ ] every parity row checked or explicitly waived by Akin
- [ ] dual-run diffs explained for at least one full weekday cycle including an Apify morning
- [ ] old plists unloaded, new plists live, old repo tagged archived

## Proof / origin

docs/cutover-plan.md

## Out of scope

To be refined before approval. This stub sequences the backlog; scope narrows or
splits when the spec is drafted for real.
