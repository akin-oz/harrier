---
spec: 017
title: Outreach draft generation
status: accepted
approved: yes
milestone: M4
depends: [016,012]
---

# Spec 017: Outreach draft generation

## Problem

Both draft paths; nothing sends.

## Scope

- AI drafts with audience inference (recruiter, hiring_manager, peer)
- template path with lint-and-repair loop and connection-note length limits
- outreach config files ported (public class; defaults containing PII go to the profile tables, ADR-008)

## Acceptance criteria

- [ ] behavior pins from tests/test_outreach_messages.py and test_generate_outreach.py pass

## Proof / origin

scripts/generate_outreach.py; scripts/outreach_messages_lib.py

## Out of scope

To be refined before approval. This stub sequences the backlog; scope narrows or
splits when the spec is drafted for real.
