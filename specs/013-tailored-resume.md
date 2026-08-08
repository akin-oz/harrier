---
spec: 013
title: Tailored resume generation with truth validation and PDF gate
status: accepted
approved: yes
milestone: M3
depends: [012,004]
---

# Spec 013: Tailored resume generation with truth validation and PDF gate

## Problem

The flagship artifact: verified content only, PDF or failure, no internal labels visible.

## Scope

- bullet pool and truth sources loaded from the encrypted data layer (content out of code)
- content plan validation (require_truth, bullet ID validation, evidence-group dedupe)
- HTML template render, Playwright PDF, validate_rendered_pdf (non-empty, no replacement chars, no placeholders, one page)
- internal metadata sidecar only; visible header scrubbing
- deterministic no-LLM fallback plan; tracker tailor verb updates the row only on PDF success

## Acceptance criteria

- [ ] behavior pins from tests/test_tailor_resume.py pass
- [ ] a run with a failing PDF leaves the tracker row unchanged

## Proof / origin

scripts/tailor_resume.py; OPERATIONS.md Tailored Resume Generation

## Out of scope

To be refined before approval. This stub sequences the backlog; scope narrows or
splits when the spec is drafted for real.
