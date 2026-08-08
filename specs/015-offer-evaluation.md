---
spec: 015
title: Offer evaluation and batch prospect evaluation
status: accepted
approved: yes
milestone: M3
depends: [012,004]
---

# Spec 015: Offer evaluation and batch prospect evaluation

## Problem

The 6-block evaluation with its machine verdict contract, and the batch driver with auditable auto-reject.

## Scope

- evaluation blocks A-F, verdict contract (strong_apply, apply, borderline, skip, confidence, deal_breakers)
- report output and story capture with dedupe into a bounded store
- batch driver over prospects; auto-reject only with an explicit opt-in flag, audit-logged

## Acceptance criteria

- [ ] a skip verdict above threshold rejects only with the flag set and writes an audit entry
- [ ] reports gate re-runs as today

## Proof / origin

scripts/evaluate_offer.py; scripts/evaluate_prospects.py

## Out of scope

To be refined before approval. This stub sequences the backlog; scope narrows or
splits when the spec is drafted for real.
