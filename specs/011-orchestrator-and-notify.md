---
spec: 011
title: Discovery orchestrator, summaries, Telegram notify
status: accepted
approved: yes
milestone: M2
depends: [008,009,010]
---

# Spec 011: Discovery orchestrator, summaries, Telegram notify

## Problem

One run over all sources in priority order with one aggregated notification.

## Scope

- priority order greenhouse, ashby, lever, remoteok, apify, wellfound, wttj
- per-source and aggregate run summaries (incoming/ shapes preserved)
- single Telegram message gated on new prospects and notify flag
- --scheduled policy flag: Apify weekday-mornings-only and configured count (one config value)

## Acceptance criteria

- [ ] dry-run over fixtures produces the aggregate summary
- [ ] notify sends exactly one message for N sources

## Proof / origin

scripts/run-job-imports.py; scripts/run-all-intake.sh

## Out of scope

To be refined before approval. This stub sequences the backlog; scope narrows or
splits when the spec is drafted for real.
