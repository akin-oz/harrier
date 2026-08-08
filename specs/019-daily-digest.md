---
spec: 019
title: Daily digest
status: accepted
approved: yes
milestone: M4
depends: [018,016]
---

# Spec 019: Daily digest

## Problem

The 20:30 Telegram summary.

## Scope

- new prospects today, top prospects, outreach actions due, ghosted applications (>21 days), actionable mail events
- dry-run mode; date override for backfill

## Acceptance criteria

- [ ] digest over fixtures renders all five sections
- [ ] dry-run sends nothing

## Proof / origin

scripts/send_daily_digest.py

## Out of scope

To be refined before approval. This stub sequences the backlog; scope narrows or
splits when the spec is drafted for real.
