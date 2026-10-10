---
spec: 087
title: A version number the truth sources state after the same technology needs no claim
status: accepted
approved: yes
milestone: M8
depends: [034, 065, 066, 068, 085, 086]
---

# Spec 087: A version number the truth sources state after the same technology needs no claim

## Problem

Rule C5 of spec 065 treats every number token as a figure that needs
evidence. That is right for "processed 1,200 invoices" and wrong for
"Kafka 3". A version number is part of a technology's name, and the prompt
already allows versions "the material supplies" (spec 065, prompt
instructions).

Today a version passes C5 only if its number is in the company name or role
title, or if some declared claim whose sentence holds the same number cites
evidence holding it, with the rate read from that evidence line matching
(`_number_violations` in `services/api/src/harrier/apply/claims.py`). The
claim does not have to be the version's own sentence, but its sentence must
hold the number.

Models do not do that reliably for the sentences where versions usually sit:
a one-line summary of the stack. A cover letter run was refused twice,
before and after the spec 085 retry, with `number without evidence` on a
version. Both responses opened the short version with a stack summary
holding a technology name and its version. Both left that sentence
undeclared, and both cited the truth line holding the version on a different
claim whose sentence did not hold the number. The truth sources stated the
version. The run failed on bookkeeping.

The obvious fix, "a number passes if the phrase before it is in the truth",
is not safe. A truth line "Led 3 teams" would ground "led 3 squads". Each
condition below exists because a looser one let an invented claim through.
The cases are named in Behavior and pinned by the planned tests.

## Scope

- `_number_violations` in `services/api/src/harrier/apply/claims.py`, and a
  helper beside it that finds version occurrences in a line.
- Tests in `services/api/tests/test_apply_claims.py`.
- Spec 065: the C5 and C6 rows gain the exemption, and the lessons table row
  "Time-sensitive tool details" is updated.
- No change to the prompts, the refusal text (spec 086 owns it), the CLI, the
  API or the contract. No new input: `ClaimContext` already carries the truth
  sources and the skill vocabulary.

## Behavior

### V0. Where occurrences are read

The exemption applies to C5's own tokens: `number_tokens` over the text C5
already reads. For each token, V1 is evaluated on the token's line of that
text, with two changes:

- each C10 placeholder is replaced by the word `[[placeholder]]` instead of
  a space, so a term before a placeholder never names a number after it;
- inline markup is removed (`strip_inline_markup`, spec 068).

Each token is mapped to its word in that line. A token that cannot be mapped
is not exempt.

Truth lines are read with inline markup removed, one line at a time.

### V1. A version occurrence

A number in a line is a version occurrence when all of these hold.

1. **Shape.** Its word matches
   `^[(\["“]*\d{1,3}(?:\.\d+)*[)\]"'”’.,;:!?]*$`: one to three digits,
   optional `.digits` groups, and only brackets, double quotes or end
   punctuation around them. So `3`, `3,`, `(3)`, `20` and `3.12` qualify.
   These never do: `2024` (a year), `'24` (a short year), `1,200`, `$40k`,
   `10x`, `40%`, `3+`, `3/Spring`, `3-based`, `3.x`.
2. **Named.** The words before it on the same line end with a term from the
   skill vocabulary (`SkillVocabulary.terms`: `all_skills`, and the keys and
   aliases of `technology_aliases`), separated from the number by spaces
   only. The term starts the line, or follows whitespace, an opening bracket
   or a double quote. So `non-Kafka 2` and `my.kafka 3` are not named by
   `Kafka`. A term of one or two characters is found case-sensitively, a
   longer one case-insensitively, as C8's `_word_pattern` does. When more
   than one term ends there, as `Apache Kafka` and `Kafka` do in
   `Apache Kafka 3`, the longest one names the occurrence.
3. **Not a duration or a count of times.** Unless the number's word ends a
   sentence (as `_SENTENCE_END` reads it), the next word, with surrounding
   punctuation removed and lowercased, is not one of `year`, `years`, `yr`,
   `yrs`, `month`, `months`, `week`, `weeks`, `day`, `days`, `hour`, `hours`,
   `time`, `times`. So `Kafka 3 years` is not a version, and `Kafka 3. Day to
   day` is.
4. **No rate.** `number_tokens` reads no rate on this token when it reads
   that line alone. Reading the line alone matters: over the joined text,
   the C6 window runs across a field that has no end punctuation. A short
   version ending "(Kafka 3, Django)" followed by a full version that opens
   with "Every" reads that `3` as a rate.
5. **Ends the phrase (output only).** The number's word ends with `,`, `.`,
   `;`, `:`, `!`, `?`, `)`, `]` or a closing quote, or is the last word of its
   line, or the next word is `and`, `or`, `with`, `in`, `on`, `to` or `for`.
   So `(Kafka 3, Django)`, `Kafka 3 and Django` and `on Kafka 3.` qualify,
   and `Kafka 3.6 million events`, `Kafka 3 plus years`, `make 3 dashboards`
   and `Kafka 500 topics` do not, because a number before a noun can be a
   count. Truth lines are not held to this, so "Led the Kafka 3 upgrade"
   still grounds `Kafka 3`.

### V2. A grounded version

An output version occurrence is grounded when some supporting line of the
truth sources holds a version occurrence (V1.1 to V1.4) with:

- the same naming term, spelled with exactly the same characters, case
  included, as in the output. So "In spring 2 engineers joined" does not
  ground `Spring 2`, and `Apache Kafka 3` is not grounded by a line naming
  only `Kafka 3`;
- the same `NumberToken.raw`, character for character. Surrounding brackets
  and punctuation are not part of it, so `3,` matches `3.`, but `3` does not
  match `3.4`, and `3.4` does not match `3`.

Supporting lines are the lines `TruthSources.contains` reads
(`services/api/src/harrier/resume/content.py`): the truth and achievements
documents, plus the brief evidence and views that `with_operator_evidence`
adds (spec 066, B4 and B7). Lines under a "claims I must not make" heading
are not supporting lines (spec 034). Within a supporting line, the denied
text (spec 100) grounds no version; the asserting text before it does.
"Moved to Kafka 3 without downtime" grounds `Kafka 3`, and "Ran Kafka in
production. Never ran Kafka 3." does not
(`services/api/tests/test_apply_claims.py::test_a_version_is_grounded_only_by_asserting_text`).

These never ground a version:

- a supporting line that C7's marker matches (`synthetic` or `demo`),
  whatever the output sentence says. Otherwise "Built a demo pipeline on
  Kafka 3" would ground "I ran Kafka 3 for paying clients" with no claim.
  Today C5 refuses that sentence when it is undeclared, and C7 refuses it
  when it is declared with the demo line as evidence;
- any line of the posting, the application profile or the candidate
  document. A version the employer uses is not one the candidate has used;
- the `verified_skills` list. It names skills, not the lines that prove them.

### V3. The exemption

A grounded output occurrence is skipped by C5 and C6, at that occurrence only.

- Another occurrence of the same value is checked as today. "I ran Kafka 3
  and led 3 workshops" still refuses the second `3` unless a claim covers it.
- The company and role exemption, and the claim path, do not change.
- An occurrence that is not grounded goes through C5 and C6 as today, with
  the spec 086 refusal text.

### V4. What does not change

- The prompts.
- The "To verify" section (spec 066, B9). `render_review` in
  `services/api/src/harrier/apply/review.py` lists each distinct number token
  once, by its raw text, so an exempt version is still listed as
  `- Number: 3`.
- C8. The naming term is still checked as a skill.

## Failure modes

These are decisions. Each is a consequence of V0 to V3.

- **A count after a technology in the truth.** Truth "Ran Kafka 500
  partitions" grounds output "I ran Kafka 500." The output form must end the
  phrase (V1.5), but the truth form need not, so the truth's noun is never
  compared.
- **A capitalised term that is also a word.** With `Spring` in the
  vocabulary, truth "Joined the team in Spring 24 as a contractor" grounds
  output "I build services in Spring 24." A two-digit year without an
  apostrophe passes V1.1.
- **An exempt version's sentence is not listed as a claim.** It appears under
  "To verify" only as a number, without its sentence.
- **The vocabulary decides.** Only a term without the version names a
  version. A vocabulary that lists `Python 3` but not `Python` gives no
  exemption to `Python 3.12`. `config/resume-content.example.json` lists one
  technology with its version in both the alias key and `all_skills`, and
  that one passes only through its bare alias.
  A technology missing from `all_skills` and `technology_aliases` gets no
  exemption either. The operator adds the bare term. Those fields also feed
  resume ranking (`services/api/src/harrier/resume/ranking.py`), fit
  evaluation (`services/api/src/harrier/resume/evaluation.py`), offer
  evaluation (`services/api/src/harrier/offers/evaluate.py`) and C8
  (`_skill_violations` in `services/api/src/harrier/apply/claims.py`).
- **Polarity is spec 100's rule** (`SENTENCE_NEGATORS`,
  `EXCLUSION_MARKERS` and `denial_start` in
  `services/api/src/harrier/resume/content.py`). "Haven't run Kafka 3 in
  production" is denied, so it grounds nothing. "Moved to Kafka 3 without
  downtime" grounds `Kafka 3`, because only the text after "without" is
  denied
  (`services/api/tests/test_apply_claims.py::test_a_version_is_grounded_only_by_asserting_text`).
- **Grounded versions that are still refused, even through a claim:**
  `Kafka 3` against a truth that says only `Kafka 3.6`, `Kafka 3-based` or
  `Kafka 3/Spring 6`. No fragment cut from those lines holds a standalone
  `3`, so a declared claim gets `number without evidence` or `number changed
  scope`, depending on what it quotes. The model has to write the version the
  truth states. Also refused through a claim: an output `Kafka 3 a year ago`,
  which `number_tokens` reads as a rate, so a claim gets `number changed
  scope`.
- **Grounded versions that are still refused without a claim, and pass with
  one as today:** `Apache Kafka 3` against `Kafka 3`; `kafka 3` against
  `Kafka 3`; `Kafka 2 and 3` (the `3` has no term before it); `Kafka 3
  components` (a noun follows); versions named by a year, such as
  `SQL Server 2019`; a truth line that puts the term and the number in
  separate table cells (`| Kafka | 3 |`), in underscore emphasis
  (`_Kafka 3_`) or in link text (`[Kafka 3](url)`); a truth line where a rate
  word falls within three words after the version ("Moved to Kafka 3 and
  shipped every week"), which a claim turns into `number changed scope`.

## Data and privacy

No new data is read or sent. The truth sources and the vocabulary are already
loaded for C2 and C8. Examples in this spec and its tests are synthetic.

## Acceptance criteria

Every test stubs `generate_text` with synthetic responses and goes through
`generate_cover_letter` or `generate_answer_set`. Each seeds a synthetic truth
document and a vocabulary holding `Kafka`, `Apache Kafka`, `Django`, `Spring`,
`Go` and `make`, with `Kafka`, `Apache Kafka`, `Django` and `Spring` in
`verified_skills`, so C8 never refuses the same draft. Every refusal test
asserts the exact spec 086 number refusal, not only that the draft is
refused. Each test fails when the condition it names is removed.

- [x] An undeclared "Kafka 3" in a short version without end punctuation,
      followed by a full version whose first word is "Every", passes when the
      truth says "Ran the event pipeline on Kafka 3"
      (`services/api/tests/test_apply_claims.py::test_a_version_the_truth_states_needs_no_claim`)
- [x] The same in an answer
      (`services/api/tests/test_apply_claims.py::test_a_grounded_version_passes_in_an_answer`)
- [x] "Kafka 4" against a truth that says "Kafka 3" is refused
      (`services/api/tests/test_apply_claims.py::test_an_invented_version_is_refused`)
- [x] "Kafka 3" is refused against truth lines "Kafka 30", "Kafka 3.6" and
      "Kafka 3-based"
      (`services/api/tests/test_apply_claims.py::test_a_version_inside_a_longer_number_is_not_grounded`)
- [x] "Kafka 3.6" against a truth that says "Kafka 3" is refused
      (`services/api/tests/test_apply_claims.py::test_a_more_precise_version_than_the_truth_is_refused`)
- [x] "led 3 squads." against "Led 3 teams" is refused. V1.5 refuses that
      output on its own, because a noun follows the number, so the test also
      shows "Of the teams, I led 3." refused against "Hired and led 3 teams",
      where only the vocabulary decides: "led" is not a vocabulary term.
      The second case was added by the implementing change
      (`services/api/tests/test_apply_claims.py::test_a_number_after_a_word_outside_the_vocabulary_still_needs_a_claim`)
- [x] "Kafka 3 and led 3 workshops" refuses the second `3`
      (`services/api/tests/test_apply_claims.py::test_a_grounded_version_does_not_exempt_the_same_number_elsewhere`)
- [x] "I run Kafka 3, every week." is not exempt: the comma ends the
      phrase, and "every" is a rate
      (`services/api/tests/test_apply_claims.py::test_a_version_with_a_rate_is_not_exempt`)
- [x] "Kafka 3.6 million events", "Kafka 3 plus years" and "make 3
      dashboards" are refused against truth lines holding "Kafka 3.6",
      "Kafka 3" and "Helped make 3 senior hires"
      (`services/api/tests/test_apply_claims.py::test_a_number_followed_by_a_noun_is_not_exempt`)
- [x] "Kafka 3 years" is refused, a truth line "Kafka 3 years" grounds
      nothing, and "on Kafka 3. Day to day" is a version
      (`services/api/tests/test_apply_claims.py::test_years_after_a_version_are_not_a_version`)
- [x] "Spring 2024" is refused against "Moved in Spring 2024", "Spring 24"
      against "Joined in Spring '24", "Kafka $40k" against "Cut the Kafka
      $40k bill", and "Django 10x" against "Made Django 10x faster"
      (`services/api/tests/test_apply_claims.py::test_years_money_and_multipliers_after_a_term_are_not_versions`)
- [x] A truth line with "demo" grounds no version, both in a sentence
      without "demo" and in one that says "demo"
      (`services/api/tests/test_apply_claims.py::test_a_demo_line_does_not_ground_a_version`)
- [x] A version found only in the posting, only under the disclaimer
      heading, only on a negated truth line ("Did not ship Kafka 3 to
      production"), only in the application profile or only in
      `verified_skills` is refused, and one given only as brief evidence
      passes
      (`services/api/tests/test_apply_claims.py::test_only_supporting_truth_lines_ground_a_version`)
- [x] "**Kafka** 3" in the output is grounded by "Kafka 3"
      (`services/api/tests/test_apply_claims.py::test_a_marked_term_still_names_its_version`)
- [x] "Kafka [[TODO: cluster]] 3" is not a version
      (`services/api/tests/test_apply_claims.py::test_a_placeholder_between_term_and_number_breaks_the_phrase`)
- [x] "non-Kafka 2" and "my.kafka 3" are not named by "Kafka"
      (`services/api/tests/test_apply_claims.py::test_a_word_ending_in_a_term_does_not_name_a_version`)
- [x] "In spring 2 engineers joined" does not ground "Spring 2", and "go 1"
      is not named by `Go`
      (`services/api/tests/test_apply_claims.py::test_the_term_must_be_spelled_the_same_in_the_truth`)
- [x] "Apache Kafka 3" is not grounded by a truth line naming only "Kafka 3"
      (`services/api/tests/test_apply_claims.py::test_the_longest_term_names_the_version`)
- [x] Spec 065's C5 and C6 rows state the exemption, and its lessons row
      reads "C5 catches a standalone version number unless the truth sources
      state it after the same technology (spec 087)".
- [x] `just check` passes

The tests are in `services/api/tests/test_apply_claims.py`.

## Honest limitations

- This weakens C5 on purpose. A sentence whose only number is a grounded
  version no longer has to be declared, so it is no longer checked by C1 and
  C2. The words around the version are checked only by the rules that read
  all output text: C5 and C6 for any other number, C8, C9, N1's banned
  phrases, the forbidden-phrase check and the never-name check.
- The motivating run passes only if the operator's vocabulary holds the
  technology's bare name. That cannot be checked from this repository,
  because the vocabulary is personal data.
- Whether models still produce other number refusals after this is not
  measured.

## Proof / origin

The motivating run's state and exit code are in the local run journal
(`data/runs/journal.jsonl`), which holds no response text. Its two responses
are in the opt-in provider log (`data/llm-logs/`). Its first refusal is the
spec 085 R6 WARNING line in `data/logs/harrier.log`. The second refusal went
to stderr and the GUI log only, and is not kept. All three files are
never-in-git.

The current rule is `_number_violations` in
`services/api/src/harrier/apply/claims.py`. An unclaimed number is refused
today, as pinned by `test_a_number_absent_from_its_evidence_is_refused` (a
percentage). The planned tests above pin the same refusal for counts, years,
money and multipliers after a vocabulary term.

Each condition in V0 to V2 answers a case found while this spec was
designed, by running an uncommitted prototype against `number_tokens` and
the existing generator tests. The prototype is not the proof. The planned
tests are, and each one fails when the condition it names is removed.

## Out of scope

- Comparing the noun after the number in the truth line (see Failure modes).
- Aliases grounding each other, ranges such as `Kafka 2 and 3`, and `+`
  versions such as `Kafka 3+`.
- Versions the employer states. An employer claim citing the posting already
  covers them.
- Changing the claim path. Today one claim citing a value clears every
  occurrence of that value in the unit (`_number_violations`). That gap is
  not fixed here. It is not recorded in any spec yet and needs its own, with
  the acceptance criterion that a claim clears a number only in the sentence
  it declares.
- Changing `NEGATIONS`, or how C6 reads a rate in the joined text for
  numbers that are not versions.
- Listing an exempt version's grounding line under "To verify".
