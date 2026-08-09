---
spec: 012
title: LLM provider seam
status: in-progress
approved: yes
milestone: M3
depends: [001]
---

# Spec 012: LLM provider seam

## Problem

The old repo had one LLM client (scripts/llm_client.py) and two bypasses
that constructed OpenAI clients directly (evaluate_offer.py,
tailor_resume.py). Every M3 feature (tailoring, cover letters, offer
evaluation) needs LLM calls; all of them must go through one seam so
provider selection, fallback, and debug logging behave identically
everywhere, and so the bypass class of bug cannot recur.

## Scope

- Package harrier.llm with a facade: generate_text(system_prompt,
  user_input, model=None, provider=None, timeout=300) and
  load_config(provider=None, model=None) -> LLMConfig.
- Four providers, ported from the old client:
  - codex-cli: local codex exec, read-only sandbox, output-last-message
    tempfile, model flag only when a model is set
  - claude-cli: local claude -p with JSON envelope parsing; strips
    ANTHROPIC_API_KEY from the child env unless CLAUDE_CLI_USE_API_KEY
    is truthy, so subscription auth is the default
  - openai-api: OpenAI Responses API
  - anthropic-api: Anthropic Messages API (ANTHROPIC_MAX_TOKENS honored,
    default 4096)
- Provider selection: AI_PROVIDER with the old aliases (codex, claude,
  openai, anthropic, and underscore or squashed variants), unknown values
  raise LLMClientError. Model resolution order: explicit argument, then
  AI_MODEL, then the per-provider env (CODEX_CLI_MODEL, CLAUDE_CLI_MODEL,
  OPENAI_MODEL, ANTHROPIC_MODEL), then the provider default (gpt-5-mini,
  empty for codex, sonnet for claude-cli, claude-sonnet-4-5).
- auto (the default): fallback chain in the old order, codex-cli if its
  binary resolves, then openai-api if OPENAI_API_KEY is set, then
  anthropic-api if ANTHROPIC_API_KEY is set, then claude-cli if its
  binary resolves; codex-cli is the last resort when nothing is
  configured. A provider that errors or returns empty output falls
  through to the next; when all fail, LLMClientError carries every
  provider's error.
- Binary resolution: PATH lookup plus the old fallback locations, with
  CODEX_CLI_PATH and CLAUDE_CLI_PATH overrides.
- Debug call logging, opt-in via AI_DEBUG or the per-provider debug envs,
  appends JSONL to the data directory (data/llm-logs/{provider}.log,
  never-in-git per ADR-008). API keys are never written to logs.

## Stated changes from the old code

- No .env reading inside the domain: the old client loaded .env on every
  call. In harrier the CLI's load_project_env owns that (spec 011); the
  domain reads os.environ only.
- Both API providers use urllib.request from the stdlib. The old client
  imported the openai package for one call and requests for another; the
  repo's design rule prefers the stdlib, and screening.http already set
  the idiom.
- An explicitly passed model is honored in auto mode. The old client
  dropped the model argument during auto fallback unless AI_MODEL was
  also set, which is a bug, not a behavior worth pinning.
- Debug logs move from logs/ (old repo) to the data directory, which is
  root-anchored gitignored, so opt-in logging can never leak into git.
- The auto-fallback notice is a logging.warning, not a print to stderr.

## Acceptance criteria

- [ ] Behavior pins ported from the old tests/test_llm_client.py pass:
      codex-cli default config, auto picks openai-api on key when codex
      is missing, provider aliases plus AI_MODEL override, per-provider
      model env without AI_MODEL, auto fallback after a provider error
      hits providers in order with their default models
- [ ] Unknown provider raises; a fixed provider returning empty output
      raises; auto with all providers failing raises with every error
      named
- [ ] No module outside harrier.llm imports harrier.llm.providers
      (import-linter contract)
- [ ] All gates green on PR

## Proof / origin

Old repo scripts/llm_client.py and tests/test_llm_client.py; bypasses in
scripts/evaluate_offer.py and scripts/tailor_resume.py. Proving file:
services/api/tests/test_llm.py.

## Out of scope

Streaming responses, token accounting, retries beyond the auto chain,
per-task model routing (specs 013 to 015 pass model or provider when they
need to), and moving provider settings into the database (spec 023).
