---
spec: 014
title: Cover letters and application answers
status: accepted
approved: yes
milestone: M3
depends: [013]
---

# Spec 014: Cover letters and application answers

## Problem

The remaining recruiter-facing artifacts with their style and PDF gates.

## Scope

- cover letters: 3 paragraphs, banned phrases stripped and validated, PDF required, PDF/HTML contain only the full letter
- answers: deterministic template path and AI path, style rules, saved markdown outputs
- application profile load/validate and profile check gate

## Acceptance criteria

- [ ] behavior pins from tests/test_openai_cover_letters.py, test_draft_application_answers.py, test_application_profile.py pass

## Proof / origin

scripts/openai_cover_letters.py; scripts/application_answers_lib.py

## Out of scope

To be refined before approval. This stub sequences the backlog; scope narrows or
splits when the spec is drafted for real.
