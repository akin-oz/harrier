---
spec: 086
title: A number refusal names the sentence its number was read from
status: accepted
approved: yes
milestone: M8
depends: [065, 085]
---

# Spec 086: A number refusal names the sentence its number was read from

## Problem

Rules C5 and C6 of spec 065 refuse a number by naming only the number:
`number without evidence: 3`, `number changed scope: 1,200`
(`_number_violations` in `services/api/src/harrier/apply/claims.py`). Most
other refusals name something that can be found:

- C4 and C7 quote the sentence the model declared, and C1 quotes a declared
  sentence that is missing from the text. All three can be found in the
  model's claims list.
- C9 quotes the piece `_SENTENCE_SPLIT` cuts from the joined text.
- C2 and C3 quote an evidence fragment from the model's claims list.
- C8, N1's banned-phrase check, the forbidden-phrase check and the
  never-name check (spec 066, B1) name a word or phrase.

A bare digit cannot be found. A letter can hold "3" in a version, a count and
a date at once.

That matters because of spec 085. The retry sends the refusal list back to
the model and asks it to fix every entry. A cover letter run was refused with
`number without evidence: 3` and nothing else. The model had written a
technology name and its version in a sentence it did not declare as a claim.
It had cited the truth line holding that version on a different claim, whose
sentence did not hold the number, so the citation did not count. The retry
received only the digit, wrote the same sentence again, and was refused the
same way. The operator reading the run log had the same problem.

Two more things hide in the bare form today:

- The same value in two sentences is refused once, because `check_claims`
  drops duplicate strings. The model learns about one place at most.
- C6 decides per occurrence. One `1,200` can keep its scope while another
  becomes a rate. The refusal does not say which.

## Scope

- `_number_violations` in `services/api/src/harrier/apply/claims.py`, and a
  helper beside it that finds the sentence for a token.
- Tests in `services/api/tests/test_apply_claims.py`.
- Spec 065's C5 and C6 rows: the refusal column shows the new form.
- No change to which numbers are refused, to the prompts, to
  `RETRY_INSTRUCTION`, to the CLI, to the API or to the contract.

## Behavior

### S1. The form

```
number without evidence: <token> (in: <sentence>)
number changed scope: <token> (in: <sentence>)
```

`<token>` is `NumberToken.raw`, exactly as today (`3`, `1,200`, `40%`). The
text before the first colon does not change, so anything that matches on the
rule name still matches.

### S2. Which sentence

The sentence comes from the text `_number_violations` already reads: the
unit's texts joined by a blank line, with C10 placeholders removed. That
string is split in two steps:

1. at every line break;
2. within each line, at `_SENTENCE_SPLIT`, the split C9 already uses
   (whitespace after `.`, `!` or `?`).

The sentence is the piece that holds the token's word, with whitespace
collapsed to single spaces and trimmed. It is quoted whole, never cut.

Step 1 keeps fields apart. `normalize_cover_letter_text`
(`services/api/src/harrier/apply/letters.py`) puts the short version on one
line and each full-version paragraph on its own line. `sanitize_answer_text`
(`services/api/src/harrier/apply/answers.py`) puts each short answer, medium
answer and note on one line. `check_claims` joins the fields with a blank
line. So a field without end punctuation is not quoted together with the
next one, as C9's quote can be today.

### S3. A repeated value in one sentence

When `number_tokens`, reading that sentence alone, returns more than one
token with the refused token's `value`, the refusal also names the word
before the refused occurrence:

```
number without evidence: 3 (after "of" in: I ran Kafka 3 with a team of 3.)
```

So `40%` and `40` count as the same value, and the `3` inside `300` does not.
The word is the one before the token in that sentence, with surrounding
punctuation removed the way C5 removes it. When the refused occurrence is the
first word of its sentence, the form is `(first word of: <sentence>)`. A
value that occurs once in its sentence always uses `(in: <sentence>)`, even
when it is the first word.

### S4. Verdicts do not change

`number_tokens` still reads the whole checked text, so every token, every
rate and every verdict is the same as today. Only the refusal text is new.
The split points in S2 all fall on whitespace, so each word of the text lands
in exactly one piece.

### S5. How many refusals

There is one refusal per distinct string. The existing `dict.fromkeys` in
`check_claims`, and the one across answers in `_checked_answers`, drop exact
duplicates and keep the first. So:

- the same value refused in two different sentences gives two refusals
  (today: one);
- the same sentence in both letter versions, or in two answers, gives one
  refusal, and its quote finds both copies;
- order is text order, as today.

### S6. Where the text goes

The new strings travel unchanged in `ClaimCheckError.violations`, in the
retry's `refusals` list (spec 085, R2), in the retry WARNING line (spec 085,
R6), and in the CLI's `cover letter failed:` and `answers failed:` lines.

## Failure modes

- **An abbreviation splits early.** "e.g." ends a piece under
  `_SENTENCE_SPLIT`, so the quote can be shorter than the sentence. It is
  still verbatim and can still be searched for.
- **A placeholder in the sentence.** The quote comes from the text with
  placeholders removed, as C9's does, so it is not verbatim in the previous
  response. Placeholders are only reported (C10) when nothing refuses, so a
  refused unit can still hold one.
- **Long log lines.** A sentence with several refused numbers is quoted once
  per number. Nothing caps it, because a cut quote cannot be searched for. A
  sentence holding `; ` reads ambiguously in the log and in stderr, which join
  refusals with `; `. The retry payload is a JSON list and is not ambiguous.
- **Two answers with the same sentence.** The refusal does not name the
  answer. If one answer declared the sentence and the other did not, the
  refusal can read as wrong. Spec 085 left the answer number out, and this
  spec keeps it out.

## Data and privacy

The quoted sentence is the model's own output, so the retry still sends the
provider nothing it did not write (spec 085, Data and privacy).

The quote also reaches places that are not all redacted:

- When `configure_logging` can load its values, the retry WARNING passes the
  identity redaction filter (`services/api/src/harrier/logredact.py`). That
  filter replaces the `name`, `email`, `phone` and `linkedin` of the
  `candidate` block in the `resume_data` document, and the contacts'
  `person_name`, `person_email` and `linkedin_url`. When it cannot load them,
  the WARNING is not redacted (`services/api/src/harrier/logsetup.py`). The
  filter never covers `never_name` values from the brief.
- The CLI's final `cover letter failed:` and `answers failed:` lines are
  printed straight to stderr (`services/api/src/harrier_cli/main.py`), with
  no redaction. The run manager streams them to the browser after removing
  credential-shaped values only (`scrub_event_data` in
  `services/api/src/harrier_api/runs.py`).

C1, C4, C7 and C9 already quote sentences into all of these places. This spec
adds sentences that hold a refused number. Logs, run journals and drafts are
never-in-git. Redacting the CLI's failure output is not done here; see Out of
scope.

## Acceptance criteria

Every test stubs `generate_text` with synthetic responses and goes through
`generate_cover_letter`, `generate_answer_set` or the CLI, never a helper
alone.

- [ ] A letter whose short version holds two sentences, the second with an
      unclaimed number, is refused with exactly
      `number without evidence: <n> (in: <the second sentence>)` (planned
      test_a_number_refusal_names_its_sentence)
- [ ] A claimed total restated as a rate is refused with exactly
      `number changed scope: <n> (in: <that sentence>)` (planned
      test_a_scope_refusal_names_its_sentence)
- [ ] One unclaimed value in two different sentences gives two refusals, one
      per sentence (planned
      test_the_same_number_in_two_sentences_is_refused_in_each)
- [ ] One sentence in both the short and the full version gives one refusal
      (planned test_a_sentence_in_both_letter_versions_is_refused_once)
- [ ] A sentence holding the value twice names the word before the refused
      occurrence, and `40%` with `40` counts as the same value (planned
      test_a_repeated_number_in_one_sentence_names_the_word_before_it)
- [ ] A sentence holding the value twice, whose refused occurrence is its
      first word, uses `(first word of: <that sentence>)`, and a value that
      occurs once as the first word uses `(in: <that sentence>)` (planned
      test_a_repeated_number_that_opens_its_sentence_says_first_word)
- [ ] A short version without end punctuation is quoted alone, not joined
      to the full version (planned
      test_a_field_without_end_punctuation_is_its_own_sentence)
- [ ] The retry's `refusals` list carries the `(in: ...)` form (planned
      test_the_retry_receives_the_sentence_of_a_number_refusal)
- [ ] The existing tests that assert a C5 or C6 refusal pass unchanged:
      `test_a_number_absent_from_its_evidence_is_refused`,
      `test_a_total_rewritten_as_a_rate_is_refused`,
      `test_a_total_rewritten_with_a_slash_suffix_is_refused`,
      `test_a_percentage_without_its_sign_is_refused`,
      `test_every_violation_is_listed_in_one_refusal` and
      `test_a_second_refusal_fails_with_its_own_violations`. Each matches the
      rule name with `in`, and the rule name is unchanged.
- [ ] Spec 065's C5 and C6 rows show the new form in the refusal column.
- [ ] `just check` passes

The planned tests go in `services/api/tests/test_apply_claims.py`.

## Honest limitations

- This makes a number refusal findable. It does not make the model fix it.
  Whether the retry now succeeds on number refusals is not measured.
- Two sentences that differ only in a removed placeholder quote the same,
  and are refused once.
- A sentence is whatever S2 cuts. It is a split on punctuation and line
  breaks, not a grammar.

## Proof / origin

The motivating run's state and exit code are in the local run journal
(`data/runs/journal.jsonl`). Its first refusal, `number without evidence: 3`
and nothing else, is the spec 085 R6 WARNING line in `data/logs/harrier.log`.
Its two responses are in the opt-in provider log (`data/llm-logs/`). The
second refusal went to stderr and the GUI log only, and is not kept. All
three files are never-in-git. Reading the two responses showed the same
undeclared sentence holding the version in both attempts.

The bare form is built in `_number_violations` in
`services/api/src/harrier/apply/claims.py`.

## Out of scope

- Which numbers are refused. Spec 087 changes that for version numbers.
- Adding a sentence to any other refusal. C1, C4 and C7 quote a declared
  sentence, C9 quotes its own piece, and C2 and C3 quote an evidence
  fragment. C8, N1's banned-phrase check, the forbidden-phrase check and the
  never-name check name a word or phrase, and they refuse every occurrence,
  so naming one sentence would invite fixing only one. N1's other refusals
  quote a paragraph or count a whole field.
- Naming the answer a refusal came from (kept out by spec 085).
- Changing C9's sentence split, so the module has one definition of a
  sentence.
- Redacting the CLI's failure lines or the run event stream. That gap exists
  today for C1, C4, C7 and C9. It is not recorded in any spec yet and needs
  its own, with the acceptance criterion that the `cover letter failed:` and
  `answers failed:` lines pass the identity redaction filter before they
  reach stderr.
- Changing `RETRY_INSTRUCTION` or the prompts.
