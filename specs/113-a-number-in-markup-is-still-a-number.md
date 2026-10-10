---
spec: 113
title: A number written in inline code or underscore emphasis is still a number
status: accepted
approved: yes
milestone: M8
depends: [065, 068, 086, 087]
---

# Spec 113: A number written in inline code or underscore emphasis is still a number

## Problem

Rule C5 of spec 065 refuses a number in a letter or answer that no claim's
evidence holds. C6 refuses a number whose rate changed. Both read the number
tokens of the text (`number_tokens` in
`services/api/src/harrier/apply/claims.py`). A token is a word stripped of the
characters in `_SURROUNDING`, then matched against `_NUMBER`.

`_SURROUNDING` holds brackets, quotes, sentence punctuation, `+`, `~` and `*`.
It does not hold the backtick or the underscore. So a number written as inline
code or in underscore emphasis is not a token, and C5 and C6 never see it.
Run against the code at the spec 087 merge (a33f323), on 2026-10-10:

| Text | `number_tokens` returns |
|---|---|
| ``I led `12` engineers.`` | nothing |
| `I led _12_ engineers.` | nothing |
| `I led *12* engineers.` | `12` |
| `I led ~~12~~ engineers.` | `12` |

A letter that says ``I led `12` engineers.`` with no claim behind the number
passes C5. The PDF renders the backticks as code and the reader sees "12".
That is the invention C5 exists to stop.

Spec 068 made claim matching ignore backticks and paired asterisks. Its Out of
scope section left C5 and C6 alone: "They tokenize the raw text and use
neither comparison." So how a number inside markup is read was never written
down. This spec writes it down.

The gap also makes spec 087's version exemption coarser than it needs to be.
087 reads each output line with inline markup removed, and C5 reads it with
markup kept. Where the two readings find different numbers, 087 exempts
nothing on that line (`_grounded_versions`). The line
``Ran **Kafka** 3 and `2` replicas.`` is one: the markup-free reading finds `3`
and `2`, C5 finds only `3`. So a `3` the truth sources state is refused, and
the invented `2` passes.

A code review of the 086 and 087 changes found this on 2026-10-10.

## Scope

- `_SURROUNDING` in `services/api/src/harrier/apply/claims.py` gains the
  backtick and the underscore.
- Tests in `services/api/tests/test_apply_claims.py` and
  `services/api/tests/test_apply_brief.py`, which already reads the review
  checklist.
- Spec 065's C5 row names the two characters. Spec 068's Out of scope line on
  C5 and C6 gains a dated sentence pointing here.
- No change to `_NUMBER`, to the rate rule, to `strip_inline_markup`, to the
  prompts, to `RETRY_INSTRUCTION`, to the CLI, to the API or to the contract.

## Behavior

### M1. Backticks and underscores surround a number

When a word is stripped before it is matched as a number, backticks and
underscores are stripped with the other surrounding characters. Any run length
counts, on either side.

| Word | Token after this spec |
|---|---|
| `` `12` `` | `12` |
| ``` ``12`` ``` | `12` |
| `_12_` | `12` |
| `__12__` | `12` |
| `` `1,200` `` | `1,200` |
| `` `40%` `` | `40%` |
| `` `3.11`. `` | `3.11` |

Only the ends of a word are stripped, as today. A word with a backtick or
underscore inside it is unchanged: `1_000` and ``a`12`b`` are not number
tokens, before and after.

### M2. Every reader of number tokens gets the same tokens

`number_tokens` is the one place a number is read, so every rule that reads
it sees the new tokens:

- **C5.** A number in inline code or underscore emphasis needs a claim whose
  sentence and evidence hold it, like any other number.
- **C6.** Its rate is read the same way. Evidence written in markup counts:
  a claim quoting the truth line ``Cut `40%` of costs`` cites `40%`.
- **Spec 086 refusals.** The refusal names the stripped token and quotes the
  sentence as written: ``number without evidence: 12 (in: I led `12`
  engineers.)``. When the refusal names the word before a repeated number, that
  word is stripped the same way, so ``(after "Kafka" in: ...)`` rather than
  ``(after "`Kafka`" in: ...)``.
- **Spec 087.** A version occurrence is still decided on the markup-free
  line. Because C5 now finds the same numbers there, a line like
  ``Ran **Kafka** 3 and `2` replicas.`` no longer loses its exemption: `3` is
  exempt when the truth states it, and `2` is refused without a claim.
- **The review checklist** (spec 066, B9). `render_review` lists every number
  token as `- Number: <token>`, so a number in inline code is listed too.

### M3. Verdicts that do not change

A text with no backtick and no underscore at either end of a numeric word gets
the same tokens, and so the same refusals, as before. Words that mix digits
and letters stay non-numbers.

## Failure modes

- **A number the model puts in markup to dodge C5.** Refused, as any other
  unclaimed number. This is the point of the change.
- **A claim whose sentence keeps the backticks.** C1 already ignores
  backticks when it finds the claim sentence in the output (spec 068), and
  the token values compare equal, so the claim cites the number.
- **A technical identifier that is all digits in inline code**, such as a
  port (`` `8080` ``) or an error code (`` `404` ``). It is now a number
  token, and a letter that names it needs a claim, as it would if written
  without backticks. Before this spec it passed only because of the
  backticks.
- **Snake case with a numeric end**, such as `retry_3`. Not a token: the
  underscore is inside the word and `retry` mixes in letters.
- **Underscores used for emphasis in the truth document.** Spec 068 still
  leaves `_x_` in claim matching. That is a separate limit; see Out of scope.

## Data and privacy

No new data is read or sent. The refusal quotes the model's own sentence, as
spec 086 already does. Examples in this spec and its tests are synthetic.

## Acceptance criteria

Every test below goes through `generate_cover_letter` or
`generate_answer_set` with `generate_text` stubbed to synthetic responses. The
review checklist test reads the `## To verify` section of the markdown draft
the letter writes.

- [x] a letter saying ``I led `12` engineers.`` with no claim is refused with
      ``number without evidence: 12 (in: I led `12` engineers.)``
      (`services/api/tests/test_apply_claims.py::test_a_number_in_inline_code_needs_a_claim`)
- [x] the same with `_12_` is refused with the matching refusal
      (`services/api/tests/test_apply_claims.py::test_a_number_in_underscore_emphasis_needs_a_claim`)
- [x] a letter saying ``I led `12` engineers.``, declared as a claim whose
      evidence holds `12`, passes
      (`services/api/tests/test_apply_claims.py::test_a_claimed_number_in_inline_code_passes`)
- [x] a claim quoting a truth line ``Processed `1,200` invoices`` cannot be
      rewritten as ``1,200 invoices a month``: refused with `number changed
      scope`
      (`services/api/tests/test_apply_claims.py::test_a_rate_read_from_markup_keeps_its_scope`)
- [x] ``Ran **Kafka** 3 and `2` replicas.``, with `Kafka 3` in the truth
      sources and no claim, is refused for `2` only
      (`services/api/tests/test_apply_claims.py::test_a_number_in_markup_no_longer_drops_a_grounded_version`)
- [x] a repeated number after a word in inline code names the word without
      its backticks
      (`services/api/tests/test_apply_claims.py::test_the_word_before_a_repeated_number_drops_its_markup`)
- [x] the draft's `## To verify` section lists a number written in inline
      code as `- Number: 12`
      (`services/api/tests/test_apply_brief.py::test_the_review_lists_a_number_in_inline_code`)
- [x] `1_000`, ``a`12`b`` and `retry_3` are still not number tokens
      (`services/api/tests/test_apply_claims.py::test_markup_inside_a_word_does_not_make_a_number`)
- [x] with the backtick and underscore removed from `_SURROUNDING` again, the
      first, second, fifth and seventh tests fail, and the pull request
      records the run
- [x] spec 065's C5 row and spec 068's Out of scope line say what this spec
      changed
- [x] `just check` passes

The tests are in `services/api/tests/test_apply_claims.py`, apart from the
review checklist test, which is in `services/api/tests/test_apply_brief.py`.

## Honest limitations

- Only inline code and underscore emphasis are covered. A number inside link
  text (`[12](url)`) or an HTML tag is still not a token, because `]` and `(`
  are stripped but the link target joins the word.
- A number split by markup inside the word, such as ``1`2` ``, is not read.
- A port or status code in inline code now needs a claim (Failure modes). If
  that turns out to be common in letters, an exemption is its own spec.

## Proof / origin

- `_SURROUNDING`, `_NUMBER` and `number_tokens` in
  `services/api/src/harrier/apply/claims.py` at a33f323.
- The probe in Problem, run against that code with the project's virtual
  environment on 2026-10-10.
- Spec 068, Out of scope: C5 and C6 left to the raw text.
- Spec 087, Behavior: the line with differing readings exempts nothing.
- The code review of the 086 and 087 changes, 2026-10-10.

## Out of scope

- Underscore emphasis in claim matching (spec 068 leaves `_x_`).
- Link text, HTML tags and escaped characters around numbers.
- Number words ("twelve"), which C5 never matched (spec 065).
- Any exemption for ports, status codes or other identifiers.
- Removing markup from generated output.
