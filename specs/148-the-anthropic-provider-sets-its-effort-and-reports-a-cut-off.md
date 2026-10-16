---
spec: 148
title: The Anthropic provider sets its effort and reports a cut-off answer
status: accepted
approved: yes
milestone: M10
depends: [012, 058, 106, 107]
---

# Spec 148: The Anthropic provider sets its effort and reports a cut-off answer

## Problem

The `anthropic-api` provider sends a fixed request: model, `max_tokens`,
system prompt and one user message (`_generate_anthropic_api` in
`services/api/src/harrier/llm/providers.py`). It sends no effort, caps the
answer at 4,096 tokens unless `ANTHROPIC_MAX_TOKENS` says otherwise
(spec 012), and returns the text blocks it finds without reading
`stop_reason`.

That was safe for the default model, `claude-sonnet-4-5`
(`DEFAULT_ANTHROPIC_MODEL` in `services/api/src/harrier/llm/config.py`),
which does not think unless asked. It is not safe for the current models.
Claude Sonnet 5.5, Claude Opus 5.5 and Claude Haiku 5.5 think by default,
and their thinking counts against `max_tokens`. On a hosted account the
owner picks the model in the AI credential's optional model field, which
reaches the run as `AI_MODEL` (`credential_variables` in
`services/api/src/harrier_api/runenv.py`). With one of those models:

- A tailored resume or a cover letter can stop at the cap. The provider
  returns the truncated text as if it were the whole answer, and the
  caller's parsing or its gates refuse it, with a message about the
  content rather than the cap.
- The thinking depth is the model's default effort: `high` on Claude
  Sonnet 5.5, `medium` on Claude Opus 5.5 and Claude Haiku 5.5. Nothing in
  harrier chooses it, so the cost and the cut-off risk change with the
  model.
- A refusal (`stop_reason: "refusal"`) also comes back as whatever text
  there is, usually none, and reads as an empty answer.

So the newer models, which are cheaper per token than the default's
generation, cannot be used reliably for generation, locally or hosted.

## Scope

1. The `anthropic-api` provider sends an effort on every request to a
   model that accepts one, from a new setting, `ANTHROPIC_EFFORT`.
2. Its default output cap rises from 4,096 to 16,000 tokens.
3. It reads `stop_reason` and fails with a clear error when the answer was
   cut off at the cap or refused, instead of returning partial text.
4. A hosted run carries `ANTHROPIC_EFFORT` from the API's environment.

## Behavior

### The effort

- `ANTHROPIC_EFFORT` takes `low`, `medium` or `high`, case-insensitive,
  surrounding space ignored. Unset or empty means `medium`. Any other value
  raises `LLMClientError` naming the variable and the three values, before
  any request, as an invalid `ANTHROPIC_MAX_TOKENS` does today.
- The request carries `"output_config": {"effort": "<value>"}` when the
  configured model accepts effort, and no `output_config` otherwise.
- **Which models accept effort.** A table in
  `services/api/src/harrier/llm/providers.py` names the model id prefixes
  that do: `claude-fable-`, `claude-mythos-`, `claude-opus-5`,
  `claude-opus-4-8`, `claude-opus-4-7`, `claude-opus-4-6`,
  `claude-opus-4-5`, `claude-sonnet-5`, `claude-sonnet-4-6`, and
  `claude-haiku-5`. Every other model id, `claude-sonnet-4-5`,
  `claude-haiku-4-5` and any id the table does not name included, gets no
  effort and behaves exactly as today. The three values offered are the
  ones every model in the table accepts.
- `medium` is the default because the work is short, structured
  generation that the truth gate checks afterwards; `high` is there when a
  model's output is measured to need it.
- The provider sends no `thinking` field: each model's own default holds.
  Text blocks are read as today; `thinking` blocks are ignored.

### The output cap

`ANTHROPIC_MAX_TOKENS` keeps its rules (a positive integer, else
`LLMClientError`). Unset, it is 16,000 instead of 4,096: thinking now
shares the cap, and 16,000 keeps a non-streaming request well inside the
API's own timeouts. The request stays non-streaming.

### The stop reason

After a response, the provider reads `stop_reason`:

| `stop_reason` | Result |
|---|---|
| `end_turn`, `stop_sequence`, absent | The text blocks, joined, as today |
| `max_tokens` | `LLMClientError`: "the anthropic-api answer was cut off at max_tokens (`<n>`); raise ANTHROPIC_MAX_TOKENS or lower ANTHROPIC_EFFORT" |
| `refusal` | `LLMClientError`: "the anthropic-api model declined the request (`<category>`)", where the category is `stop_details.category` or `unspecified` |
| anything else | `LLMClientError` naming the value |

None of these is `LLMTransientError`: the same request would end the same
way, so spec 058's one retry does not run. In `auto` mode the error falls
through to the next provider, as any provider error does (spec 012). The
debug log, when on, records the error as it records a failed request
today. No message carries the prompt, the answer or the key.

### Hosted

`ANTHROPIC_EFFORT` joins `COPIED_VARIABLES` in
`services/api/src/harrier_api/runenv.py`, beside `ANTHROPIC_MAX_TOKENS`, so
a hosted run takes the value set on the API app. It is one value for every
owner. The model stays the owner's choice, through the AI credential.

## Failure modes

| Case | Result |
|---|---|
| `ANTHROPIC_EFFORT=extreme`, or `xhigh` | `LLMClientError` naming the variable and its three values; no request |
| A model id the table does not name | No effort sent; today's request, today's behavior |
| A model in the table that the API then refuses the effort for | The API's 400, as an `LLMClientError` with its detail (spec 058, not retried); the table entry is wrong and is fixed in code |
| An answer cut off at the cap | `LLMClientError` naming the cap and both settings; nothing partial is returned |
| A refusal | `LLMClientError` naming the category; nothing partial is returned |
| `auto` mode and the Anthropic answer is cut off | The next provider in the chain runs, as for any provider error |
| A hosted run with `ANTHROPIC_EFFORT` set on the API app | The run's request carries it |
| A scheduled run | Takes the value from the scheduler app's environment, which `bind_run` copies as it copies `ANTHROPIC_MAX_TOKENS`; no scheduled job generates text today |

## Acceptance criteria

- [x] With `ANTHROPIC_EFFORT` unset and the model `claude-sonnet-5-5`, the
      request body carries `"output_config": {"effort": "medium"}`; with
      `high` it carries `high`; with the model `claude-sonnet-4-5` it
      carries no `output_config` (`services/api/tests/test_llm.py::test_the_anthropic_request_carries_effort_only_for_models_that_take_it`).
- [x] An `ANTHROPIC_EFFORT` outside `low`, `medium`, `high` raises
      `LLMClientError` naming the variable, before any request (`services/api/tests/test_llm.py::test_an_invalid_anthropic_effort_raises_before_any_request`).
- [x] With `ANTHROPIC_MAX_TOKENS` unset the request's `max_tokens` is
      16,000 (`services/api/tests/test_llm.py::test_the_default_anthropic_output_cap_is_16000`).
- [x] A response with `stop_reason: "max_tokens"` raises `LLMClientError`
      naming the cap, returns no text and is not retried; one with
      `"refusal"` raises naming the category (`services/api/tests/test_llm.py::test_a_cut_off_or_refused_anthropic_answer_raises_and_is_not_retried`).
- [x] A response with `stop_reason: "end_turn"` and a `thinking` block
      before its text returns the text alone, as today (`services/api/tests/test_llm.py::test_a_thinking_block_is_ignored_and_the_text_returned`).
- [x] A hosted run's environment carries `ANTHROPIC_EFFORT` when the API's
      environment does, and the copied-variables list test names it
      (`services/api/tests/test_hosted_runs.py::test_the_run_environment_carries_the_anthropic_effort`).
- [x] Spec 012's line for `anthropic-api` names the effort and the new
      default cap, and `.env.example` documents `ANTHROPIC_EFFORT`,
      commented, with its default.
- [x] `just check` passes.

Tests stub the HTTP call; no test reaches the Anthropic API.

## Out of scope

- The effort of `openai-api` (the Responses API's reasoning effort),
  `claude-cli` and `codex-cli`. Each is its own change.
- Changing the default model (`claude-sonnet-4-5`). Decision 1.
- Per-owner effort on a hosted account. Decision 2.
- `xhigh` and `max`: not every model in the table accepts them, and short
  generation does not need them.
- Streaming the request, prompt caching, and the Anthropic SDK in place of
  the raw HTTP call.
- Measuring harrier's token use per resume or letter. No measured number is
  stated here.

## Honest limitations

- The table of models that take effort is hand-kept. A model released later
  is not in it, so it gets no effort until the table names it; that keeps
  today's behavior rather than risking a 400.
- 16,000 tokens is a ceiling chosen to sit well inside non-streaming
  timeouts, not a measurement of how much a resume or letter needs. A model
  that thinks at length on a long posting can still reach it, and then
  says so (the `max_tokens` error) instead of returning partial text.
- `medium` is chosen, not measured. The truth gate and the PDF gate still
  judge every answer; a lower effort that produces more refusals costs
  retries, and that would show in the runs.

## Decisions

Akin agreed both on 2026-10-16 and approved the spec the same day.

1. **The default model stays `claude-sonnet-4-5`.** A change to
   `claude-sonnet-5-5` is its own spec, written after a few resumes and
   letters on it have been compared. This spec does not touch
   `DEFAULT_ANTHROPIC_MODEL`.
2. **Effort is one server-wide value.** `ANTHROPIC_EFFORT` is read from the
   environment, as `ANTHROPIC_MAX_TOKENS` is. The AI credential keeps its
   shape (spec 116); the Settings page does not change.

## Migration

None required. Unset, `ANTHROPIC_EFFORT` is `medium` for the models that
take it; the default model takes none, so a user on the default sees only
the higher output cap. A user who set `ANTHROPIC_MAX_TOKENS` keeps their
value.

## Proof / origin

- Akin asked in chat on 2026-10-16 for a spec that sets the effort
  explicitly, after choosing the model for hosted generation.
- The request today: `_generate_anthropic_api` and `_anthropic_max_tokens`
  in `services/api/src/harrier/llm/providers.py`; spec 012's provider
  list.
- The model a hosted run uses: the AI credential's `model` field becomes
  `AI_MODEL` in `credential_variables`,
  `services/api/src/harrier_api/runenv.py`; `COPIED_VARIABLES` in the same
  file.
- Errors and the one retry: spec 058 (`_post_json` in
  `services/api/src/harrier/llm/providers.py`).
- Anthropic's API reference as of 2026-10-16: `output_config.effort` takes
  `low`, `medium`, `high` (and `xhigh`, `max` on some models); Claude
  Sonnet 4.5 and Claude Haiku 4.5 reject it; Claude Sonnet 5.5, Claude
  Opus 5.5 and Claude Haiku 5.5 think by default, with default effort
  `high`, `medium` and `medium`; thinking counts against `max_tokens`;
  `stop_reason` values include `max_tokens` and `refusal`, the latter with
  `stop_details.category`.
