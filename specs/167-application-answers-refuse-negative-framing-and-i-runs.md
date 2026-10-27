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

- [x] An answer set whose medium answer says "I had to" is refused naming
      L18 and answer 1, retried once, and passes when the retry drops it
      (`services/api/tests/test_answer_tone.py::test_an_answer_with_negative_framing_is_retried`).
- [x] A medium answer with two consecutive "I" sentences is refused; a
      short answer starting with "I" is not
      (`services/api/tests/test_answer_tone.py::test_i_runs_read_the_medium_answer_only`).
- [x] The deterministic path is not tone checked
      (`services/api/tests/test_answer_tone.py::test_deterministic_answers_are_not_tone_checked`).
- [x] The letter tests for L18 to L20 from spec 165 pass unchanged after
      the move (`services/api/tests/test_letter_lint.py`).
- [x] Passive growth in a short answer is refused naming answer 1 short
      (amendment 2;
      `services/api/tests/test_answer_tone.py::test_passive_growth_in_a_short_answer_is_refused`).
- [x] "e.g. my" inside a medium answer does not end a sentence for L20
      (amendment 1;
      `services/api/tests/test_answer_tone.py::test_i_runs_split_sentences_as_the_answer_limits_do`).

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

## Amendment (2026-10-27, implementation)

The build found these places where the Behavior was silent.

1. **L20 on an answer splits sentences as the answers limits do.** The
   letter's splitter ends a sentence at any `.` followed by a space, so
   "e.g. the lint step" became two sentences and a one-sentence answer
   read as "1 of 2". The answers limits (spec 066, B2) already split only
   before an uppercase letter or a quote. `brief.py` gains
   `split_sentences`, which `count_sentences` and `tone_violations` both
   use. The letter's L20 keeps its own splitter, so its behavior does not
   change. Scope gains `services/api/src/harrier/apply/brief.py`.
2. **The message names the answer, the field and the rule.** A violation
   reads `answer <n> <short|medium>: <the letter's message>`, for example
   `answer 1 medium: negative framing: had to`. "Naming L18" in the first
   criterion means the L18 message; no rule reports its id today. L20 on an
   answer says `sentences`, not the letter's `body sentences`.
3. **A one-sentence medium answer that opens with "I" is refused.** One of
   one is more than a third. The prompt asks for 3 to 6 sentences in a
   medium answer, so this is a refusal the retry can fix. Two fixtures in
   `services/api/tests/test_apply_brief.py` were one-sentence medium
   answers opening with "I" and are reworded; what they test is unchanged.
   Scope gains that file.
