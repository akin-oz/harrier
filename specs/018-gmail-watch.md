---
spec: 018
title: Gmail watch and classification
status: accepted
approved: yes
milestone: M4
depends: [004]
---

# Spec 018: Gmail watch and classification

## Problem

Readonly Gmail polling with the rule cascade and Telegram alerts.

## Scope

- OAuth setup flow; poll with lookback and dedupe state; classification cascade with the current kinds and ignore lists
- tracker row matching; event log consumed by the digest; state migration from the old repo

## Acceptance criteria

- [ ] behavior pins from tests/test_gmail_watch.py pass
- [ ] dry-run prints per-message classification without sending

## Proof / origin

scripts/gmail_watch_lib.py classify_message

## Out of scope

To be refined before approval. This stub sequences the backlog; scope narrows or
splits when the spec is drafted for real.
