---
spec: 167
title: Application answers refuse negative framing, passive growth and runs of "I" sentences
status: accepted
approved: yes
milestone: M9
depends: [014, 065, 085, 165]
---

# Spec 167: Application answers refuse negative framing, passive growth and runs of "I" sentences

## Problem

Spec 165 gives the cover letter three tone rules: no negative framing
(L18), no passive voice about one's own growth (L19), and no runs of
sentences that start with "I" (L20). The application answers generator
(`services/api/src/harrier/apply/answers.py`) writes into the same
employer's form and checks none of them. `_checked_answers` refuses banned
phrases, forbidden phrases, redacted names, limits and unbacked claims, and
nothing about tone.

The University of Edinburgh Careers Service gives the same advice for
application forms as for letters: frame a missing area as work in progress
("I'm currently working towards"), never "I don't have" (see Proof /
origin).

## Scope

- `services/api/src/harrier/apply/letterlint.py`: L18, L19 and L20 move
  into a function `tone_violations(text, *, i_runs: bool) -> list[str]`
  that `lint_letter` and the answers both call. The letter's behavior does
  not change.
- `services/api/src/harrier/apply/answers.py`: `_checked_answers` calls it;
  `ANSWERS_SHARED` states the three rules.
- Tests: `services/api/tests/test_answer_tone.py` (new). The answers block
  hash in `services/api/tests/test_academic_documents.py` changes on
  purpose.

## Behavior

- `_checked_answers` runs L18 and L19 over each draft's short and medium
  answer, and L20 over the medium answer, on every track kind. A violation
  names the answer number and joins the others in the `ClaimCheckError`, so
  the set gets the one retry spec 085 gives every refusal.
- L20 on an answer reads every sentence of the medium answer: two
  consecutive sentences starting with I, I'm, I've, I'd, I'll or My, or more
  than a third of them, are refused.
- `ANSWERS_SHARED` gains three lines: never frame a fact negatively; write
  growth in the active voice; do not start two sentences in a row with "I"
  or "My". A missing area is stated as what the candidate is doing about it,
  only when the truth sources state that.
- The deterministic answer path (`build_deterministic_draft`) is not
  checked: its text is the operator's own templates.

## Failure modes

- **A short answer of one sentence starting with "I".** L20 does not read
  short answers, so it passes.
- **The retry is refused too.** Exit 1 with every violation, as today
  (spec 085).

## Acceptance criteria

- [ ] An answer set whose medium answer says "I had to" is refused naming
      L18 and answer 1, retried once, and passes when the retry drops it
      (planned test in test_answer_tone.py:
      test_an_answer_with_negative_framing_is_retried).
- [ ] A medium answer with two consecutive "I" sentences is refused; a
      short answer starting with "I" is not (planned test:
      test_i_runs_read_the_medium_answer_only).
- [ ] The deterministic path is not tone checked (planned test:
      test_deterministic_answers_are_not_tone_checked).
- [ ] The letter tests for L18 to L20 from spec 165 pass unchanged after
      the move (`services/api/tests/test_letter_lint.py`).

## Proof / origin

- University of Edinburgh Careers Service, "Completing an application
  form" (https://careers.ed.ac.uk/cvs-and-applications/completing-an-application-form),
  tip 6: address an area you lack as work in progress.
- Same, "Writing a cover letter", tip 7: negative tone, passive voice,
  repeated "I" openings.
- `_checked_answers` and `ANSWERS_SHARED` in
  `services/api/src/harrier/apply/answers.py`.

## Out of scope

- Competency questions and STAR structure (spec 166).
- L21 (copied fact) and L22 (paragraph openers) on answers. Answers are
  short and often must restate one fact; those rules are the letter's.
- Hedging in answers.

## Honest limitations

- Phrase lists. "It fell to me to" is negative framing and passes.
