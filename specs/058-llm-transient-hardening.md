---
spec: 058
title: LLM calls survive one transient failure and tolerate trailing commas
status: accepted
approved: yes
milestone: M8
depends: [014]
---

# Spec 058: LLM calls survive one transient failure and tolerate trailing commas

## Problem

Every AI generation in the system is a single attempt, and any transient slip
discards the whole run. Two observed failures show both halves:

- A cover-letter run where the model returned a complete, valid letter with
  one trailing comma before the closing brace. Strict `json.loads` in
  `parse_cover_letter_response`
  (`services/api/src/harrier/apply/letters.py`) rejected it and the run
  failed. The content was fine; one character killed it.
- A cover-letter run where the API closed the connection mid-response after
  roughly three minutes. The claude CLI exited 1, the provider seam raised
  `LLMClientError`, and the run failed with no retry, even though a call
  minutes earlier had succeeded.

The same one-shot shape exists at every AI call site: cover letters
(`apply/letters.py`), application answers (`apply/answers.py`), outreach
drafts (`outreach/drafts.py`), offer evaluation (`offers/evaluate.py`), and
resume tailoring (`resume/ai.py`, which fails soft to the untailored order).
The operator's only recourse is to notice the failure and click again, which
usually works, which is the definition of a retry the software should have
done itself.

Retrying is safe here by construction: every one of these calls produces a
draft or an ordering. Nothing auto-sends (product invariant), so a repeated
attempt can waste at most one API call's cost, never a recruiter-visible
action.

## Scope

- A transient error class and one bounded retry inside
  services/api/src/harrier/llm/ (config.py, providers.py, `generate_text`
  in `__init__.py`). No new module boundary: callers keep importing the
  facade.
- One shared tolerant JSON parse helper, a new module under
  services/api/src/harrier/llm/, and the five parse sites switching their
  `json.loads` call to it. No other line in those five files changes.
- No CLI verb, no API route, no contract change, no schema change, no new
  configuration.

## Proof / origin

The two motivating failures are recorded in never-in-git operator data:
the run journal (data/runs/journal.jsonl) and the raw provider exchanges
in data/llm-logs/ (opt-in debug logging, spec 012). Run identifiers and
dates stay in those local files (privacy rule). Replaying the logged
trailing-comma output through `parse_cover_letter_response` reproduces the
reported error at the same character position; the dropped-connection
envelope carries `terminal_reason: "api_error"` with 5 output tokens
written when the stream died. The single-attempt
behavior being changed is pinned today by
services/api/tests/test_llm.py, and the strict parse by
tests/test_apply.py, tests/test_outreach_drafts.py, tests/test_offers.py,
and tests/test_resume.py.

## Behavior

Two independent changes, one per failure class.

### 1. One bounded retry at the provider seam

`generate_text` (`services/api/src/harrier/llm/__init__.py`) retries a failed
attempt exactly once before raising. A retry happens only for transient
failures:

- CLI provider exited non-zero (covers the mid-response connection drop)
- CLI provider timed out (`subprocess.TimeoutExpired`)
- API provider returned HTTP 429, a 5xx, or a connection error
- the provider returned an empty response

A retry never happens for failures that will repeat identically:

- provider binary not found
- missing or invalid API key or configuration
- unknown provider name

The retry waits 2 seconds, then repeats the same request unchanged. When the
retry succeeds, the caller sees a normal success; a warning log line records
that attempt 1 failed and with what error (redaction rules unchanged). When
the retry also fails, `generate_text` raises `LLMClientError` exactly as
today, with the second failure's detail.

In `auto` provider mode the existing behavior (fall through the provider
list) is preserved; the single retry applies within each candidate provider
before moving to the next.

Opt-in debug logging (`data/llm-logs/`) records each attempt as its own
entry, as it already does per call.

### 2. Trailing-comma tolerance in AI response parsing

All five AI JSON parse sites route their extracted JSON text through one
shared parse helper in `services/api/src/harrier/llm/` before interpreting
fields:

- `apply/letters.py` `parse_cover_letter_response`
- `apply/answers.py` `parse_answers_response`
- `outreach/drafts.py` `parse_ai_outreach_response`
- `offers/evaluate.py` `parse_json_response`
- `resume/ai.py` tailoring parse

The helper parses strictly first. A well-formed response is never altered.
Only when strict parsing fails does it attempt one lenient pass that removes
trailing commas immediately before a closing `}` or `]`, outside string
literals, and parses again. If the lenient pass also fails, the original
strict error propagates, so today's error messages and each site's existing
failure behavior (raise vs fail-soft) are unchanged for genuinely malformed
output.

Fenced code blocks and surrounding prose are handled as each site handles
them today; this spec changes only what happens between extraction and
`json.loads`.

## Failure modes

- **Second transient failure.** The call fails with the same `LLMClientError`
  surface as today. Worst case latency roughly doubles: two 300 second
  timeouts plus the 2 second wait. The run journal already shows the run as
  `running` throughout, so nothing new to display.
- **Deterministic failure misread as transient.** A wrong model name that the
  provider rejects with a non-zero exit gets one useless retry. Cost: one
  duplicate attempt, bounded, and both attempts land in the debug log.
- **Retry spends money.** Exactly one extra attempt, never more. No loop, no
  backoff ladder.
- **Trailing comma inside a string value.** A letter whose text legitimately
  contains `", }"` or `"],"` must not be corrupted: the lenient pass operates
  outside string literals only, and it runs only after strict parsing failed.
- **Malformed beyond trailing commas.** Truncated JSON, a bare string, or a
  missing field fails exactly as today, including the
  "failed to parse AI response" and missing-field error texts.
- **Empty response twice.** Same "returned an empty response" error as today,
  raised after the retry.
- **Concurrent runs.** Retries are per-call and hold no shared state; two
  simultaneous generations retry independently.

## Acceptance criteria

All fixtures synthetic (privacy rule); the observed trailing-comma shape is
reproduced with invented letter text, never the real content.

Retry seam (tests in `services/api/tests/`, driving `generate_text` with a
stubbed provider):

- A provider that fails once with a non-zero-exit `LLMClientError` and then
  succeeds: `generate_text` returns the second attempt's output, and the stub
  records exactly 2 attempts.
- A provider that fails twice: `generate_text` raises `LLMClientError`, and
  the stub records exactly 2 attempts, never 3.
- A binary-not-found failure: raises immediately, 1 attempt.
- An empty first response with a non-empty second: returns the second.

Parse tolerance (unit tests on the shared helper plus one test per call
site's parser):

- `{"short_version": "a...", "full_version": "b...",\n}` (trailing comma,
  both fields valid) parses; `parse_cover_letter_response` returns both
  fields.
- The same tolerance holds for `parse_answers_response`,
  `parse_ai_outreach_response`, `parse_json_response` in offers, and the
  resume tailoring parse, each proven by its own test with a trailing-comma
  fixture.
- A field value containing the literal text `", }"` survives parsing byte
  for byte.
- A trailing comma before `]` inside a nested array parses.
- A truncated response (missing closing brace) still fails with the same
  error type and message prefix as before this change.
- A well-formed response parses to an identical result with and without the
  helper (strict-first is observable: no mutation of valid JSON).

Regression:

- `uv run ruff check` and `uv run pyright` pass; `just check` passes.
- No contract change: `just contract` produces no diff (this is all behind
  the API surface).

## Out of scope

- Re-asking the model after a parse failure. A parse failure after the
  lenient pass still fails the run; regeneration is a different cost decision
  and can be its own spec if trailing commas turn out not to be the dominant
  malformation.
- More than one retry, exponential backoff, jitter, or a circuit breaker.
- Changing `auto` mode's provider fallback order or semantics.
- Changing `resume/ai.py`'s fail-soft contract: on final failure it still
  returns `None` and the caller still falls back to the untailored order.
- Tolerating any malformation other than trailing commas (single quotes,
  comments, unescaped newlines all still fail).
- Retry for non-LLM network calls (Apify, Hunter, Gmail).
- Surfacing retry counts in the web UI or the run journal schema.

## Migration

None. No flags, no schema change, no config. Existing failures become either
a silent recovery (one warning log line) or the same error as before.
