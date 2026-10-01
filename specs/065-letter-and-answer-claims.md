---
spec: 065
title: Cover letters and application answers cite the evidence for every claim, and code checks the citations
status: accepted
approved: yes
milestone: M8
depends: [014, 034]
---

# Spec 065: Cover letters and application answers cite the evidence for every claim, and code checks the citations

## Problem

Spec 034 gave resume bullets a truth check. It did not give one to cover
letters or application answers. Its reason still holds: a letter is prose,
and matching prose sentence by sentence against a truth document either
rejects ordinary paraphrase or verifies nothing.

So today a letter (`services/api/src/harrier/apply/letters.py`) is checked
for banned phrases, bullet-list voice, at least three paragraphs, and a
valid PDF. Answers (`services/api/src/harrier/apply/answers.py`) get a
banned-phrase strip and nothing else. The prompts say "no invented
experience". No code checks that.

Worse, the letter's normalization step edits the model's text without
saying so (`normalize_cover_letter_text`):

- banned phrases are deleted as substrings, inside words too, so
  "I leveraged caching" becomes "I d caching" in a recruiter-facing letter
  (reproduced against the current code);
- paragraphs under eight words are dropped;
- only the first three paragraphs are kept;
- the text is trimmed to 240 words.

That is the silent omission spec 034 removed from resumes, still present on
letters.

A manual session writing a real application by hand showed what goes wrong
when nothing checks the prose. The failures were concrete and checkable:

- a number that was a total over a period came back as a rate;
- a skill named in the posting was claimed although the truth source never
  showed it;
- a rule was described as "enforced" when it was a convention;
- a demo built on synthetic data was described without saying so;
- a concrete example was needed and none existed, so one was invented.

## Scope

Spec 034's objection is to matching prose against the truth document. This
spec does not do that. It changes what the model returns and checks that
instead:

1. The model returns each factual sentence it wrote as a **claim**: the
   sentence, quoted verbatim from its own output, and one or more
   **evidence** fragments, quoted verbatim from the material it was given.
2. Code checks the citations. The sentence must be in the output. Every
   candidate evidence fragment must pass `TruthSources.contains`, the spec
   034 predicate, so disclaimer sections and negated sentences cannot
   verify anything here either.
3. Code then checks the output for narrow, mechanical signals of invention
   that do not depend on the model declaring a claim: numbers, known
   technology names, enforcement words, and unfilled placeholders.
4. Normalization stops editing content. It may still fix whitespace and
   strip formatting artifacts; anything that would remove words refuses
   instead.

The model can still write an unnumbered factual sentence and not declare
it. That gap is stated under Limitations rather than claimed closed.

## Inputs

- The generated output: for a letter, `short_version` and `full_version`;
  for answers, each `short_answer`, `medium_answer` and note.
- The model's `claims` list, returned alongside the text in the same JSON.
  Shape per claim: `{"sentence": str, "about": "candidate" | "employer",
  "evidence": [str, ...]}`.
- Candidate evidence: the `TruthSources` already loaded from the profile
  store (truth and achievements documents). Only these. The model also
  receives the application profile, the candidate JSON and tracker notes,
  for framing; the prompt tells it to cite only the truth and achievements
  documents. A profile story that is not also in the truth document will
  therefore be refused. That is intended: the truth document is where a
  verified story belongs.
- Employer evidence: the job description text the run already loads.
- The resume bundle (`load_bundle`): `verified_skills`, `all_skills`,
  `technology_aliases`.

## Rules

Each rule names its refusal. A refusal names the offending text and the
rule, like spec 034. All rules run on the final text after N1, which no
longer changes words, so a sentence the model quoted is still there.

| # | Rule | Refusal |
|---|---|---|
| N1 | Normalization only collapses whitespace and removes leading bullet markers and `Paragraph N:` labels. A banned phrase, matched on word boundaries, refuses instead of being deleted. A paragraph under eight words, a fourth paragraph, or more than 240 words refuses instead of being dropped or trimmed. | `banned phrase`, `stub paragraph`, `too many paragraphs`, `over the word limit` |
| C1 | Every claim's `sentence` appears in the output, compared case-insensitively with whitespace collapsed. | `claim sentence not in output` |
| C2 | Every evidence fragment of a `candidate` claim passes `TruthSources.contains`. | `unverified evidence` |
| C3 | Every evidence fragment of an `employer` claim appears in the job description, compared the same way as C1. | `employer evidence not in posting` |
| C4 | A sentence containing a first-person word (`I`, `I'm`, `I've`, `my`, `me`) cannot be an `employer` claim. | `first-person sentence cited to the employer` |
| C5 | Every number token in the output appears in the evidence of a claim whose sentence contains it. A number token is a word, stripped of surrounding punctuation, matching an optional currency sign, digits with optional `,` or `.` separators, and an optional `k`, `m`, `%`, `x` or `/day`, `/week`, `/month`, `/year` suffix. Words mixing digits and letters (`S3`, `OAuth2`) and slash pairs (`24/7`) are not number tokens. Numbers in the company name or role title are exempt. | `number without evidence` |
| C6 | A number keeps its scope. A number token carries a rate if it has a `%` or `/period` suffix, or one of `per`, `a day`, `a week`, `a month`, `a year`, `each`, `every`, `daily`, `weekly`, `monthly`, `annually` follows within three words. The same number must carry a rate in the evidence exactly when it carries one in the output. | `number changed scope` |
| C7 | A claim whose evidence comes from a line containing the word `synthetic` or `demo` (word boundaries, so `demonstrated` does not count) must itself contain one of those words. | `synthetic evidence not labelled` |
| C8 | A technology from the bundle's vocabulary (`technology_aliases` keys and aliases, and `all_skills`) that appears in the output on word boundaries must be in `verified_skills` or be an alias of one, or pass `TruthSources.contains`. Terms that appear in the role title are exempt. | `unverified skill` |
| C9 | A sentence containing the word `enforced`, `enforces` or `enforcement` must be a claim whose evidence contains one of those words. | `enforcement claimed without evidence` |
| C10 | No unfilled placeholder remains: `[[TODO: ...]]`, or a square-bracketed span starting `insert`, `todo`, `placeholder`, `example` or `your`, case-insensitive. | `needs input` (see Outputs) |

N1 applies the same way to answers: `sanitize_answer_text` stops deleting
banned phrases and refuses instead.

The prompt (both `SYSTEM_PROMPT_BASE` strings) gains the matching
instructions: declare every factual sentence as a claim; quote evidence
verbatim and only from the truth and achievements documents or the
posting; when a concrete example is needed and none exists in the
material, write `[[TODO: what is needed]]` instead of inventing one; say
"convention" or "warning" rather than "enforced" unless the material shows
code enforcing it; do not state model names, versions or other
time-sensitive tool details unless the material supplies them.

## Outputs and failure modes

- **Pass.** Behavior as today: the letter writes markdown, HTML and PDF; the
  answers write markdown.
- **Refusal (N1, C1 to C9).** No artifact is written.
  `generate_cover_letter` and `generate_answer_set` raise `ClaimCheckError`
  (a `ValueError`) listing every violation, not only the first. The CLI
  prints them to stderr and exits 1, as it does today for other generation
  failures.
- **Needs input (C10 only).** The markdown draft is written, placeholders
  intact, so the operator can fill them. For a letter, no HTML and no PDF
  are rendered: a recruiter-facing artifact never carries a placeholder.
  The CLI prints `markdown=<path>`, then one `needs_input=<placeholder>`
  line per placeholder, and exits 3. Exit 3 is new. The run manager
  (`services/api/src/harrier_api/runs.py`) treats every non-zero exit as
  failed, so the GUI shows this as a failed run; that is stated under
  Limitations, not changed here.
- **Malformed claims.** A response with no `claims` key, or a claim missing
  `sentence` or `evidence`, is a parse failure (`failed to parse AI
  response`), like a missing letter field today.
- Failure mode this must not introduce: a letter with no factual claims at
  all is legal. C1 to C9 constrain what is claimed; they do not require a
  minimum.
- Failure mode this will introduce, on purpose: letters that pass today
  because normalization quietly cut them will refuse. A refusal costs a
  retry; a cut letter costs a sentence nobody chose to remove.

## Acceptance criteria

All tests in `services/api/tests/test_apply_claims.py`, a new file. Every
test stubs `generate_text` with a synthetic response and calls
`generate_cover_letter` or `generate_answer_set`, never a rule helper
alone, so removing a rule from the decision fails its test. Truth documents
are written inline in the test, independent of any bullet pool, so the
predicate can fail (spec 034's lesson).

| Criterion | Proof |
|---|---|
| N1 a banned phrase refuses instead of being deleted mid-word | `test_a_banned_phrase_refuses_and_is_not_deleted_mid_word` |
| N1 a long letter is refused, not trimmed | `test_a_letter_over_240_words_is_refused_not_trimmed` |
| N1 a stub or fourth paragraph refuses instead of being dropped | `test_a_stub_paragraph_refuses_instead_of_being_dropped`, `test_a_fourth_paragraph_refuses_instead_of_being_dropped` |
| N1 answers refuse a banned phrase too | `test_a_banned_phrase_refuses_the_answers` |
| C1 a claim quoting a sentence the output does not contain is refused | `test_a_claim_sentence_missing_from_the_letter_is_refused` |
| C2 a candidate claim citing text absent from the truth document is refused | `test_a_claim_with_invented_evidence_is_refused` |
| C2 evidence under a "must not claim" heading does not verify | `test_evidence_from_a_disclaimer_section_does_not_verify` |
| C2 evidence only in the application profile does not verify | `test_evidence_only_in_the_application_profile_is_refused` |
| C3 employer evidence must be in the posting | `test_employer_evidence_absent_from_the_posting_is_refused` |
| C4 a first-person sentence cannot be cited to the employer | `test_a_first_person_sentence_cited_to_the_employer_is_refused` |
| C5 a number with no evidence is refused | `test_a_number_absent_from_its_evidence_is_refused` |
| C5 company, role, mixed tokens and slash pairs are not checked | `test_numbers_in_the_company_and_role_are_exempt`, `test_mixed_tokens_and_slash_pairs_are_not_numbers` |
| C6 a total rewritten as a rate is refused | `test_a_total_rewritten_as_a_rate_is_refused` |
| C6 a total rewritten with a slash suffix is refused | `test_a_total_rewritten_with_a_slash_suffix_is_refused` |
| C6 a rate kept as a rate passes | `test_a_rate_kept_as_a_rate_passes` |
| C6 a percentage dropping its sign is refused | `test_a_percentage_without_its_sign_is_refused` |
| C7 a claim resting on synthetic evidence must say so | `test_unlabelled_synthetic_evidence_is_refused`, `test_labelled_synthetic_evidence_passes` |
| C7 "demonstrated" is not a synthetic marker | `test_demonstrated_is_not_a_synthetic_marker` |
| C8 a known technology not in verified skills is refused | `test_an_unverified_skill_is_refused` |
| C8 an alias of a verified skill passes | `test_an_alias_of_a_verified_skill_passes` |
| C8 a term from the role title is exempt | `test_a_role_title_term_is_not_a_skill_claim` |
| C9 "enforced" without evidence saying so is refused | `test_enforcement_language_without_evidence_is_refused` |
| C10 a letter with a placeholder writes markdown and no PDF | `test_a_letter_with_a_placeholder_writes_markdown_and_no_pdf` |
| C10 the CLI exits 3 and names each placeholder | `test_cli_cover_letter_exits_3_and_names_each_placeholder` |
| every violation is reported, not only the first | `test_every_violation_is_listed_in_one_refusal` |
| a response without claims is a parse failure | `test_a_response_without_claims_fails_to_parse` |
| C2 an answer citing invented evidence is refused | `test_an_answer_with_invented_evidence_is_refused` |
| C6 the rate is read from the line the evidence was quoted from | `test_a_rate_is_read_from_the_line_the_evidence_was_quoted_from` |
| C9 "enforced" backed by evidence passes | `test_enforcement_language_with_evidence_passes` |
| C10 a placeholder run removes the PDF and HTML of an earlier run, so the artifact endpoint never offers a stale PDF beside a new draft (review of #84) | `test_a_placeholder_run_removes_the_pdf_and_html_of_an_earlier_run` |
| C10 a bracketed "insert" is a placeholder too | `test_a_bracketed_insert_is_a_placeholder_too` |
| a claim with no evidence is a parse failure | `test_a_claim_without_evidence_fails_to_parse` |
| a clean synthetic letter and answer set still pass end to end | `test_a_grounded_letter_passes_every_rule`, `test_a_grounded_answer_set_passes_every_rule` |

- [x] every row above has a test that fails when its rule is removed:
      checked by disabling each rule in turn (25 mutants, all failed a test)
- [x] the existing tests in `services/api/tests/test_apply.py` pass, with
      their stubbed responses extended to include `claims`; a test that
      asserted the old trimming or dropping is changed to assert the
      refusal, and the change is named in the pull request
- [x] README.md "Application artifacts" states what the letter and answer
      gates check and what they cannot, each naming its test
- [x] no candidate content in any test or fixture; every truth document,
      posting and response in the new tests is synthetic
- [x] `just check` green

## What the implementation decided

Recorded here so the spec and the code agree.

- **The vocabulary is read from the `resume_data` fields, not through
  `load_bundle`.** `load_skill_vocabulary` in
  `services/api/src/harrier/resume/content.py` reads `all_skills`,
  `verified_skills` and `technology_aliases` directly, the same way
  `load_forbidden_phrases` reads its list. `load_bundle` validates the whole
  resume bundle, and a letter should not fail because an unrelated resume
  field is malformed. No document means an empty vocabulary; a document
  that is not valid JSON refuses, so a broken file cannot switch C8 off
  silently.
- **Answers carry claims per answer.** Each answer object has its own
  `claims` list, and C1 to C9 run on that answer's short and medium text and
  notes together.
- **Two-letter terms match case-sensitively in C8**, so a skill named `Go`
  or an alias `TS` does not match every "go" or "ts" in prose.
- **C6 reads the rate from the line the evidence was quoted from**, not
  only the quoted fragment, so quoting part of a sentence does not drop its
  rate. The rate window stops at the end of a sentence.
- **Placeholders are removed before C5 to C9 run**, so the text inside a
  placeholder is not checked as a claim.
- **The answers CLI also exits 3** when an answer holds a placeholder,
  printing `answers=<path>` and then one `needs_input=` line each.
- **Existing tests changed, not only extended.**
  test_normalize_cover_letter_text_removes_internal_dump_language (removed) became
  `test_internal_dump_language_refuses_the_letter` plus
  `test_normalize_cover_letter_text_changes_formatting_only`, and
  test_generated_answers_avoid_banned_phrases (removed) became
  `test_generated_answers_with_banned_phrases_are_refused`. Each old test
  asserted the deletion this spec removes.

## Which lessons this tests, and which stay rules

| Lesson | Tested here | Rule only (prompt) |
|---|---|---|
| Grounding: claims trace to a source | C1, C2, C3, C4 | unnumbered claims the model does not declare |
| Grounding: invented example becomes a placeholder | C10 keeps a placeholder out of the PDF | that the model writes a placeholder instead of inventing |
| Grounding: numbers keep their scope | C5, C6 | number words ("twelve") are not matched |
| Grounding: synthetic data labelled | C7 | evidence that is synthetic without saying so on its line |
| Grounding: skills only where shown | C8 | technologies outside the bundle's vocabulary |
| Enforcement language | C9 | "convention" versus "warning" wording |
| Time-sensitive tool details | C5 catches standalone version numbers | names, and versions written inside a word |

Opinion questions, anonymity, employer guidance, stated limits,
compensation and the review checklist are in spec 066, which depends on
this one.

## Data and privacy

No new path. No schema change: claims are returned and checked in memory
and are not stored. No contract change: the API runs the CLI for letters
and answers (`services/api/src/harrier_api/runs.py`, kinds `cover-letter`
and `answers`), so its surface is unchanged.

## Limitations

- **A claim the model does not declare is not checked**, unless it carries
  a number, a known technology, or an enforcement word. The gate reduces
  invention; it does not eliminate it. Read the letter.
- **Verbatim evidence is required.** A model that paraphrases its evidence
  gets refused. That is the intended trade.
- **The technology vocabulary is the candidate's own bundle.** A technology
  the bundle has never heard of, named only in the posting, is not caught
  by C8. `SKILL_SIGNALS` in the screening rules was considered and rejected
  as the vocabulary: it holds role words (`frontend`, `product engineer`)
  that every letter for such a role repeats.
- **The letter header still deletes banned phrases as substrings.**
  `strip_banned_phrases` is kept for the header, which is built from the
  tracker title rather than generated (spec 034). A title containing a
  banned word inside a longer word would be cut the same way. Not changed
  here: the header is not generated text.
- **Exit 3 shows as a failed run in the GUI.** Distinguishing "needs input"
  in the run manager would change the run state enum in the API contract,
  which is its own spec.

## Proof / origin

The README paragraph on cover letters, spec 034's scope correction, and
`normalize_cover_letter_text` are verifiable in the tree. The mid-word
deletion was reproduced by calling `strip_banned_phrases("I leveraged
caching")`, which returns `"I d caching"`. The five failure shapes come
from a manual application written by hand; no content from it appears
here. Reviewed for proportionality by `review-principal-architect`, which
moved the forbidden-phrase fix out (below), moved the no-trim rule in from
spec 066, and narrowed C5 to C8.

## Out of scope

**Forbidden phrases on the letter and answers.** Spec 034 has a ticked
criterion saying `forbidden_phrases` refuses an artifact on the resume, the
letter and the answers path. `forbidden_hits` has one production caller,
`validate_rendered_markdown` in
`services/api/src/harrier/resume/markdown.py`; the letter and answers never
call it. The correct behavior was already written down in an approved spec,
so it was fixed as a bug fix under `Spec: 034`, ahead of this spec, and is
not part of it.

Model-based or semantic verification. The deterministic answer path
(`build_deterministic_draft`, no production caller today). The resume path,
which spec 034 owns.
