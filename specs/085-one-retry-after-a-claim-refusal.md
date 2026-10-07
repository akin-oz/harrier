---
spec: 085
title: A refused letter or answer set gets one automatic retry with its refusals
status: proposed
approved: no
milestone: M8
depends: [058, 065, 066]
---

# Spec 085: A refused letter or answer set gets one automatic retry with its refusals

## Problem

Spec 065 checks every generated letter and answer set before anything is
written. A draft that breaks a rule raises `ClaimCheckError`, the CLI prints
the violations and exits 1, and the run shows as failed. The operator's only
move is to start the run again, which sends the same request with no
knowledge of what went wrong.

Some refusals are slips the model can fix in one more pass when told what
failed. An answers run started from the GUI failed on exactly one violation:
C1, `claim sentence not in output`. The model declared a sentence as a claim,
its evidence passed C2, and every other rule passed. But the answer text
worded that sentence differently from the declaration. The content was
grounded. The run failed because the model's two copies of one sentence
disagreed, and nothing told it so.

Spec 065 already lists every violation in one error "so one retry can address
all of them" (`ClaimCheckError` in `services/api/src/harrier/apply/claims.py`).
That retry has never existed. The list goes to stderr and a person has to read
it, click again, and hope.

Spec 058 added a retry at the provider seam, but only for transport failures
(a dropped connection, a timeout, an empty response). It repeats the request
unchanged. A refusal is not transient, and repeating the same request
unchanged only gets another draft with the same chance of slipping.

## Scope

- `generate_cover_letter` in `services/api/src/harrier/apply/letters.py`.
- `generate_answer_set` in `services/api/src/harrier/apply/answers.py`, and
  `generate_ai_answers`, which it calls.
- Tests in `services/api/tests/test_apply_claims.py`.
- One added sentence in spec 065, under "Outputs and failure modes".
- No CLI flag, no API route, no contract change, no schema change, no new
  configuration. The rules themselves (N1, C1 to C9, the forbidden-phrase,
  never-name and stated-limit checks) do not change.

## Behavior

### R1. One retry after a refusal

When a letter or an answer set is refused, the generator calls the model one
more time. A refusal here means anything that raises `ClaimCheckError` from
`generate_cover_letter` or `generate_answer_set` today: N1, C1 to C9, a
forbidden phrase, a redacted name, a stated limit (spec 066), and the
letter's paragraph and word rules.

The second response goes through the same parse and the same checks as the
first, from scratch. If it passes, the run continues as if it had passed the
first time: the letter writes markdown, HTML and PDF, and the answers write
markdown. The draft, and the claims listed under "To verify" (spec 066, B9),
come from the second response only.

There is exactly one retry. A second refusal is final.

### R2. What the retry sends

The system prompt is the same string as the first call. The user input is the
first call's payload, the same JSON object, with one added top-level key,
`retry`:

```json
{
  "retry": {
    "instruction": "Your previous response was refused by the checks listed in refusals. Return a complete new response in the same format that fixes every one of them. Every claim sentence must appear word for word in the text.",
    "refusals": ["claim sentence not in output: ..."],
    "previous_response": "<the first response, exactly as the model returned it>"
  }
}
```

`refusals` is the first refusal's `violations` list, in order, unchanged.
`previous_response` is the raw text `generate_text` returned, before parsing
or normalization. The instruction text is fixed and can be worded
differently, but it lives in code, not in configuration.

For answers, the retry covers every question that went to the model the first
time, not only the one that failed. Answers built by code (salary,
requirement placeholders, opinion placeholders; spec 066, B6 to B8) are not
re-sent and do not change.

### R3. The second refusal

If the second response is refused, `ClaimCheckError` is raised with the
second response's violations only, and no artifact is written, as today. The
CLI prints `cover letter failed: ...` or `answers failed: ...` and exits 1, as
today.

### R4. A pass on the first response costs nothing

A first response that passes calls the model once, exactly as today.

### R5. What is not retried here

- **Parse failures** (`failed to parse AI response`, including a malformed
  `claims` list). Spec 058 covers trailing commas. Other parse failures stay
  one attempt.
- **Transport failures** (`LLMClientError`, `AI request failed`). Spec 058
  already retries these inside `generate_text`.
- **Placeholders (C10).** A placeholder is not a refusal: the draft is written
  and the CLI exits 3. Asking the model to fill it would invite the invention
  C10 exists to stop.
- **A count mismatch** (`AI returned N answers for M questions`). It stays a
  `RuntimeError` on either attempt.

### R6. The retry is visible in the run log

Before the second call, the generator logs one WARNING line through the
module logger:

```
cover letter refused on attempt 1, retrying once: <violations joined by "; ">
answers refused on attempt 1, retrying once: <violations joined by "; ">
```

The CLI's logging (`harrier.logsetup`) sends this to stderr, which the run
manager streams to the GUI log, and to `harrier.log` through the identity
redaction filter. A run that passes on the retry exits 0, and this line is
the only sign that the first attempt failed.

## Failure modes

- **The retry passes by deleting the claim.** Spec 065 allows a letter with no
  claims, and a sentence nobody declares passes C1 unless it trips C5 to C9.
  So the model can fix a C1 refusal by dropping the claim entry and keeping
  the sentence, or by dropping the sentence. Both pass. This is the same
  limit spec 065 states under Limitations. The retry does not make it worse
  than a person clicking again, but it does make it automatic.
- **A passing answer changes on the retry.** The whole set is regenerated, so
  an answer that passed the first time can come back worded differently, or
  start failing. The second set is checked in full, so a new failure refuses
  the run (R3).
- **Transport failure on the retry.** `generate_text` raises `LLMClientError`
  after its own spec 058 retry. The run fails with `AI request failed`, exit 1.
  The first refusal is still in the log (R6).
- **Parse failure on the retry.** The run fails with `failed to parse AI
  response`, exit 1. The first refusal is in the log.
- **Cost and time.** A refused first response means one more `generate_text`
  call. With spec 058's transport retry inside each call, one run makes at
  most four provider requests. Wall time roughly doubles on runs whose first
  response is refused. Runs that pass the first time are unchanged.
- **The retry prompt is larger.** It carries the first response as well as the
  original payload. No length check is added. A provider that refuses the
  longer input fails as a transport error.

## Data and privacy

The retry sends the provider two things it did not get on the first call: the
text it returned on that call, and the violations list. Every violation quotes
text from that same response, or names a rule. A forbidden-phrase or
redacted-name violation names a list entry only because the response
contained it. Neither list is sent in full. So nothing leaves the machine that
the provider did not already produce.

The WARNING line in R6 carries the same text the CLI already prints to stderr
when a run fails today, and passes through the same redaction filter. Logs are
never-in-git.

## Acceptance criteria

Every test stubs `generate_text` with synthetic responses, and calls
`generate_cover_letter`, `generate_answer_set` or the CLI, never a helper
alone, so taking the retry out of the decision fails the test.

- [ ] A letter stub that returns a C1-refused response first and a passing
      response second gives a draft built from the second response, and the
      stub was called twice (planned
      test_a_refused_letter_is_retried_once_and_the_second_draft_is_kept)
- [ ] The same for an answer set, with the markdown written from the second
      response (planned
      test_a_refused_answer_set_is_retried_once_and_the_second_set_is_written)
- [ ] On the second call, the system prompt equals the first call's, and the
      user input parses to the first payload plus `retry`, whose `refusals`
      equals the first refusal's violations in order and whose
      `previous_response` equals the first raw response (planned
      test_the_retry_sends_the_refusals_and_the_previous_response)
- [ ] A stub that returns two different refused responses raises
      `ClaimCheckError` with the second response's violations and not the
      first's, and the stub was called exactly twice (planned
      test_a_second_refusal_fails_with_its_own_violations)
- [ ] `harrier answers` against two refused responses exits 1, prints
      `answers failed:` to stderr, and writes no file (planned
      test_cli_answers_exits_1_after_two_refusals)
- [ ] A passing first response calls the stub once (planned
      test_a_passing_first_response_calls_the_model_once)
- [ ] A first response with a malformed `claims` list fails with `failed to
      parse AI response` after one call (planned
      test_a_parse_failure_is_not_retried)
- [ ] A first response holding only a placeholder writes the draft and calls
      the stub once (planned test_a_placeholder_is_not_retried)
- [ ] A retry that passes logs one WARNING line that starts with
      `answers refused on attempt 1, retrying once:` and contains the first
      violation (planned test_the_retry_logs_the_first_refusals)
- [ ] The existing refusal tests in `test_apply_claims.py` still pass
      unchanged. Their stubs return the same refused response on every call,
      so each now refuses after two calls instead of one.
- [ ] Spec 065's "Refusal (N1, C1 to C9)" bullet gains one sentence: the
      refusal is raised after one automatic retry also refuses (spec 085).
- [ ] `just check` passes

The planned tests go in `services/api/tests/test_apply_claims.py`.

## Honest limitations

- One retry is a judgment, not a measurement. No failure rate per attempt has
  been recorded, so this spec cannot say how often the retry will rescue a
  run. If it rarely does, it only adds cost and time.
- The retry fixes slips. It does not fix a draft that is wrong because the
  truth sources lack the evidence. Asked again, the model can only rephrase,
  drop the claim, or fail again.
- The model is not told which answer a violation came from, because
  `check_claims` does not say. Most violations quote their sentence, which
  usually identifies the answer.
- Nothing in the written draft records that it came from a second attempt.
  The run log (R6) is the only record.

## Proof / origin

The motivating answers run is in the local run journal
(`data/runs/journal.jsonl`, never-in-git): state `failed`, exit code 1. Its
GUI log showed one `claim sentence not in output` violation and nothing else.
The raw model response was not kept, because LLM prompt logging is opt-in
(spec 012) and was off. So the reworded sentence cannot be shown. The only
evidence is that the rule fired.

The single-attempt behavior is in `generate_cover_letter` (letters.py) and
`generate_answer_set` (answers.py). Each raises `ClaimCheckError` straight
after the first `check_claims`. The refusal tests in
`services/api/tests/test_apply_claims.py` pin the refusal, for example
`test_a_claim_sentence_missing_from_the_letter_is_refused` and
`test_every_violation_is_listed_in_one_refusal`.

## Out of scope

- Retrying parse failures or count mismatches.
- More than one retry, or a retry count set in configuration.
- Retrying only the failing answer instead of the whole set.
- Telling the model which answer a violation belongs to.
- Recording the attempt number in the draft, the "To verify" section, or the
  run journal.
- The same retry for outreach drafts, offer evaluation or resume tailoring.
  They do not raise `ClaimCheckError`.
- Changing any rule in spec 065 or 066, including C1's comparison.
- Keeping the raw model response when debug logging is off.
