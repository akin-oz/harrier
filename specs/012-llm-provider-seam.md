---
spec: 012
title: LLM provider seam
status: accepted
approved: yes
milestone: M3
depends: [001]
---

# Spec 012: LLM provider seam

## Problem

One entry point for all LLM calls, closing the old repo's two bypasses.

## Scope

- providers: codex-cli, claude-cli, openai-api, anthropic-api; auto fallback chain
- env selection (AI_PROVIDER, AI_MODEL), binary resolution, debug call logging (opt-in, never-in-git)

## Acceptance criteria

- [ ] behavior pins from tests/test_llm_client.py pass
- [ ] no module outside harrier.llm constructs a provider client (lint-checked)

## Proof / origin

scripts/llm_client.py; bypasses in evaluate_offer.py and tailor_resume.py

## Out of scope

To be refined before approval. This stub sequences the backlog; scope narrows or
splits when the spec is drafted for real.
