---
spec: 166
title: A competency question is answered in situation, task, action, result order
status: accepted
approved: yes
milestone: M9
depends: [014, 065, 066, 085, 101]
---

# Spec 166: A competency question is answered in situation, task, action, result order

## Problem

Application forms ask competency questions: "Describe a time when you
solved a problem in a new way", "Give an example of a positive
contribution to a team". The University of Edinburgh Careers Service says
employers expect the answer in STAR order and score it against the job's
competencies (see Proof / origin):

- **Situation**, about a tenth: the context in a sentence.
- **Task**, about a tenth to a sixth: what the candidate had to achieve.
- **Action**, half or more: what the candidate did, specifically. "Your
  actions are what set you apart."
- **Result**, about a fifth to a quarter: the outcome, quantified where
  possible, and what was learned.

It gives 300 to 500 words as the usual length when a form sets no limit.

Harrier treats such a question as `generic` or `experience`
(`classify_question`, `services/api/src/harrier/apply/answers.py`). The
shared prompt asks for a medium answer of three to six sentences
(`ANSWERS_SHARED`). Neither asks for the four parts, and nothing checks
that the action carries the answer.

## Scope

- `services/api/src/harrier/apply/answers.py`: a `competency` class in
  `classify_question`; the payload marks competency questions; the
  response parser reads a `star` object for them; `_checked_answers`
  checks it; the medium answer is built from it.
- `ANSWERS_SHARED` and the three answers voices in
  `services/api/src/harrier/voices.py`: the STAR instruction.
- Tests: `services/api/tests/test_star_answers.py` (new). The answers
  prompt pins in `services/api/tests/test_academic_documents.py` change on
  purpose.

## Behavior

### Classification

`classify_question` returns `competency` when the question contains, on
word boundaries and case-insensitively, any of: describe a time, describe
a situation, tell us about a time, tell me about a time, give an example,
give us an example, provide an example, share an example, an example of
when, a time when. It is checked before every other class except
`salary` and `availability`.

### What the model returns

For a question the payload marks `competency`, each answer item carries:

```json
"star": {"situation": "string", "task": "string", "action": "string", "result": "string"}
```

Code builds `medium_answer` by joining the four strings with one space, in
that order. The model's own `medium_answer` for that question is ignored.
`short_answer` is unchanged: one to three sentences.

The prompt states: every part comes from one situation the truth sources
describe; the action is what the candidate did, in the first person; the
result states the outcome with the figure the evidence gives, when it
gives one.

### Checks

Added to `_checked_answers` for a competency answer:

- **S1** all four parts are non-empty;
- **S2** the action has more words than each of the other three parts;
- **S3** situation and task together have fewer words than the action;
- **S4** the joined medium answer is 300 to 500 words, unless the brief
  sets an answers limit (spec 066), which then replaces this range as it
  replaces every answers limit today.

Every claim in the four parts is checked as today (spec 065). A violation
joins the others in the `ClaimCheckError` and gets the one retry spec 085
gives.

### Output

The answers markdown prints the medium answer as one paragraph, as today.
The review (`render_review`) lists the four parts with their word counts
under the question, so the operator can see the balance. The parts never
print in the answers file itself.

## Failure modes

- **The truth sources hold no situation for the competency.** The model
  cannot fill the parts truthfully. The prompt tells it to return an empty
  `star` and say so in `notes`. S1 refuses the answer set, the retry
  usually returns the same, and the command exits 1 naming the question.
  The operator answers it by hand or adds the situation to the truth
  sources.
- **The form's limit is 150 words.** The brief's limit replaces S4. S2 and
  S3 still hold.
- **A question reads as competency but is not** ("Give an example of your
  notice period"). `availability` is checked first, so it is not
  misclassified. Another false match gets a STAR answer, which the
  operator can rewrite.

## Acceptance criteria

- [x] Each listed phrase classifies a question as `competency`, and "What
      is your notice period?" stays `availability` (`services/api/tests/test_star_answers.py::test_competency_questions_are_classified`).
- [x] A competency answer whose action is shorter than its situation is
      refused naming S2, and the same answer with a longer action passes
      (`services/api/tests/test_star_answers.py::test_the_action_carries_the_answer`).
- [x] A competency answer with an empty result is refused naming S1
      (`services/api/tests/test_star_answers.py::test_every_star_part_is_required`).
- [x] A 250-word competency answer is refused by S4 with no brief limit,
      and passes when the brief sets answers to at most 250 words
      (`services/api/tests/test_star_answers.py::test_the_brief_limit_replaces_the_star_range`).
- [x] The medium answer is the four parts joined in order, and the review
      lists each part's word count
      (`services/api/tests/test_star_answers.py::test_the_medium_answer_joins_the_parts`).
- [x] A non-competency question parses and is checked exactly as today
      (`services/api/tests/test_star_answers.py::test_other_questions_are_unchanged`).

## Proof / origin

- University of Edinburgh Careers Service, "Completing an application
  form" (https://careers.ed.ac.uk/cvs-and-applications/completing-an-application-form):
  the STAR shares (10, 15, 50 and 25 percent on the page; 10, 10, 60 and
  20 in the "Top Tips: Job Applications" recording), the scored
  competency questions, and 300 to 500 words as a usual answer length.
- `classify_question`, `ANSWERS_SHARED` and `_checked_answers` in
  `services/api/src/harrier/apply/answers.py`.

## Out of scope

- Interview preparation from the same stories (`harrier.offers.stories`).
- Making the story bank citable. It stays context only (spec 158,
  amendment 15).
- Checking that the result is quantified. Code cannot tell whether the
  evidence held a figure for this situation; the prompt asks for it.
- Open questions ("Why are you interested in this position?"). Their
  structure is the `interest` class's today and does not change.

## Honest limitations

- The shares are checked only as an ordering of word counts (S2, S3), not
  as the guidance's percentages, which it gives two different ways.
- Classification is a phrase list. "Walk us through a project where" is not
  in it.

## Open decisions for Akin

1. **S4's default.** 300 to 500 words is the guidance's figure for forms.
   Most forms set their own limit, which replaces it. Keep it, or default
   to no word range and rely on the brief?

## Decisions taken (2026-10-26)

Akin approved the spec as written, which settles the open decisions above
as the spec states them.

## Amendment (2026-10-27, implementation)

The build found these places where the Behavior was silent or named a file
that does not change.

1. **The voices do not change.** Scope named the three answers voices in
   `services/api/src/harrier/voices.py`. Every voice wraps `ANSWERS_SHARED`
   (`assemble`), so the STAR instruction is written once there and reaches
   all three kinds. `voices.py` does not change.
2. **How the payload marks a competency question.** The answers payload
   gains `competency_questions`, the questions sent to the model that
   classify as `competency`, in order. It is an empty list when there are
   none.
3. **A missing or malformed `star` is four empty parts,** not a parse
   error, so it reaches S1 and gets the retry. A competency answer's
   `short_answer` is still required. A `star` on any other question is not
   read.
4. **An empty part stops the checks there.** When S1 finds an empty part,
   S2 to S4 are not reported for that answer: the balance of a missing part
   says nothing S1 has not said.
5. **What "the brief sets an answers limit" means for S4.** Either
   `answers.max_words` or `answers.max_sentences` in the brief replaces the
   300 to 500 word range. Spec 066 checks that limit as before.
6. **Every S violation names the answer number and the question,** for
   example `S1: answer 1 ("Describe a time when ...") has an empty result`,
   so the exit 1 after the retry names the question (Failure modes).
7. **How the review lists the parts.** `Review` gains `star`, a tuple of
   `StarBalance` (`services/api/src/harrier/apply/review.py`).
   `render_review` lists, under "To verify", a `- STAR: "question"` line
   and one indented line per part with its word count. It lists names and
   counts, not the part text, so the parts never print apart from the
   medium answer.
8. **The deterministic path** (`build_deterministic_draft`) has no
   `competency` template in the example profiles and falls back to
   `generic`, as every class without a template does. It is not STAR
   checked.
9. **The prompt pins change on purpose.** `SHARED_BLOCK_SHA256["answers"]`
   and `INDUSTRY_PROMPT_SHA256["answers"]` in
   `services/api/tests/test_academic_documents.py` move to the new block.
