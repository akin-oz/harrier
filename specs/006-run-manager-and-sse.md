---
spec: 006
title: Run manager, SSE channel, one live dry-run import
status: accepted
approved: yes
milestone: M1
depends: [005]
---

# Spec 006: Run manager, SSE channel, one live dry-run import

## Problem

Long-running work needs start, progress, logs, cancel (ADR-004). The skeleton crosses this seam with one real run class.

## Scope

- run manager: registry, subprocess execution of CLI entry points, states, on-disk journal
- POST /runs, GET /runs, GET /runs/{id}, POST /runs/{id}/cancel
- SSE endpoint with typed events and Last-Event-ID replay
- structured progress line protocol on importer stdout
- GUI run panel: start a dry-run Greenhouse import, watch live logs, cancel

## Acceptance criteria

- [ ] a dry-run import streams log lines to the browser and lands a run summary
- [ ] cancel terminates the child process and the run ends in state cancelled
- [ ] reconnect mid-run replays missed events

## Proof / origin

docs/adr/ADR-004-long-running-work.md

## Out of scope

To be refined before approval. This stub sequences the backlog; scope narrows or
splits when the spec is drafted for real.
