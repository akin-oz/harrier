---
spec: 066
title: Each application carries a brief that shapes and constrains its letter and answers, and every draft ends with what to check
status: accepted
approved: yes
milestone: M8
depends: [014, 065]
---

# Spec 066: Each application carries a brief that shapes and constrains its letter and answers, and every draft ends with what to check

## Problem

Letter and answer generation knows two things about an application: the
tracker row and the job description. Everything else a careful human uses
when writing one by hand has nowhere to live. A manual session writing a
real application showed what was missing:

- **Names that must not appear.** Some work was done for clients who
  cannot be named, and sometimes for an employer that cannot be. Nothing
  tells the generator, and nothing checks the output.
- **The employer's own rules.** Some companies publish how they want to be
  applied to: a length, a structure, a sentence count per answer. The
  letter validator hard-codes three paragraphs and 240 words
  (`normalize_cover_letter_text` and `validate_cover_letter` in
  `services/api/src/harrier/apply/letters.py`), so it cannot follow a
  stated limit. Spec 065 makes those defaults refuse instead of trim; this
  spec lets the brief replace them.
- **Requirements only the candidate can answer.** Time zone overlap,
  travel, visa sponsorship, work authorization. Today a question about
  them goes to the model like any other question.
- **Opinions.** "Which tool do you love or hate?" has no true answer in a
  truth document. The model writes one anyway.
- **Compensation.** The salary question goes to the model, which can
  present a number as if it were advice.
- **What to check before sending.** The draft arrives with no list of what
  it rests on and no obvious first step.

## Scope

A per-job brief that the operator writes and the generator obeys. It
carries the names never to write, the employer's own length and structure
rules, extra evidence, the operator's views and salary number. Code
enforces what can be checked mechanically (B1, B2, B5 to B10); the rest
reaches the model as instructions and is listed under "rule only" below.
Every draft, with or without a brief, ends with what to check and one next
action.

## Inputs

An **application brief**: one JSON document per tracker job, stored in
`profile_documents` with kind `application_brief`, name the job id, format
`json`, written only through `put_document`
(`services/api/src/harrier/profile/store.py`). No schema change: the table
already holds named documents by kind (ADR-008).

Shape, every key optional, unknown keys refused:

```json
{
  "never_name": ["Example Client Ltd"],
  "guidance_url": "https://example.com/how-we-hire",
  "employer_guidance": "Text the operator pasted from the company's guidance.",
  "letter": {"max_words": 200, "max_sentences": 8, "paragraphs": 2},
  "answers": {"max_words": 120, "max_sentences": 4},
  "evidence": ["A verbatim fact the operator supplies for this application."],
  "views": {"Which tool do you dislike most?": "The operator's own view."},
  "compensation_number": "EUR 12,345"
}
```

The committed example is `config/application-brief.example.json`,
synthetic. Harrier never reads `config/application-brief.json`: briefs
live in the database. The name is classified anyway because an operator
editing a copy of the example will save it there, and the classification
keeps that copy out of git. It is classified never-in-git in `config/data-classification.json` and `.gitignore` before
the example is added, as
`test_every_example_config_has_its_real_name_classified` requires. Both
files are guarded paths; this spec is what covers the edit.

New CLI verbs: `harrier brief set <job_id> --file <path>` validates and
stores a brief; `harrier brief show <job_id>` prints it. `harrier
cover-letter` and `harrier answers` load the brief for their job when one
exists. Without a brief, behavior is spec 065's, plus B5 to B10.

## Behavior

| # | Behavior |
|---|---|
| B1 | **Never-name.** Each `never_name` entry is passed to the prompt as a name not to write. After generation, any entry found in any generated field (letter versions, answer fields, notes), case-insensitive on word boundaries, refuses the artifact: `named a redacted name`. The metadata lines harrier itself writes (company, role, URL) are not checked. |
| B2 | **Stated limits.** `letter.max_words`, `letter.max_sentences` and `letter.paragraphs` replace spec 065's defaults (240 words, three paragraphs, no sentence limit). `answers.max_words` and `answers.max_sentences` apply to each short and medium answer. A sentence ends at `.`, `!` or `?` followed by whitespace and an uppercase letter, or by the end of the text, so `e.g. the` and `v1.2` do not split. A draft over a limit is refused, naming the field, the limit and the count: `over the stated limit`. The limits are also sent in the prompt. |
| B3 | Moved to spec 065 (rule N1) during review: it needs no brief. |
| B4 | **Employer guidance.** `employer_guidance` is added to the prompt payload and counts as employer evidence for spec 065's rule C3. Each `evidence` entry counts as candidate evidence for rule C2, alongside the truth and achievements documents, for this job only; this is how the operator points the generator at a fact the truth document does not hold. `guidance_url` is recorded and shown; it is not fetched (see Out of scope). |
| B5 | **Hard-requirement flags.** The job description and the employer guidance are scanned for four kinds: `time_zone`, `travel`, `visa_sponsorship`, `work_authorization`. Each hit becomes a flag: the kind and the sentence it came from. Flags never filter or score; the screening invariant that EU permit phrases are positive signals is untouched. |
| B6 | **Requirement questions are not answered for you.** A question that classifies as one of the four kinds is not sent to the model. Its answer is `[[TODO: your answer]]`, with the matching flags listed under it. |
| B7 | **Opinion questions wait for your view.** A question that classifies as opinion (it contains `favourite`, `favorite`, `your opinion`, `what do you think about` or `how do you feel about`, or contains `love`, `hate` or `dislike` together with `tool`, `language`, `framework`, `library` or `technology`; so "why would you love working here" is not an opinion question) is not sent to the model unless `views` has an entry for it, matched on normalized text. Without one, its answer is `[[TODO: your own view]]`. With one, the view is sent and counts as candidate evidence. |
| B8 | **Compensation is assembled, not generated.** A salary question is never sent to the model. Its answer is built by code: `Draft for you to edit. This is not advice.`, then `Posted range:` followed by every currency range found verbatim in the posting, or `none in the posting`, then `My number:` followed by `compensation_number`, else the candidate document's `compensation.salary_target_eur`, else `[[TODO: your number]]`. |
| B9 | **Every draft ends with what to check.** The letter markdown and the answers markdown end with `## To verify` and `## Next action`. To verify lists, in order: each placeholder, each flag, each claim sentence with its evidence (spec 065), each number in the output. Next action is exactly one line, chosen in order: the first placeholder (`Replace "<placeholder>" in <path>.`), else the first flag (`Decide your answer to the <kind> requirement: "<sentence>".`), else `Read <path> once against the posting.` |
| B10 | **Recruiter-facing output stays clean.** Neither section of B9, nor any flag, appears in the letter HTML or PDF. |

Placeholders from B6, B7 and B8 make the run end in spec 065's "needs
input" outcome: markdown written, exit 3.

## Acceptance criteria

Test names below are planned and written without backticks, because
`test_every_test_a_spec_names_actually_exists` fails on a backticked name
that does not exist yet. The implementing change backticks each one, which
binds it to the check.

Tests in `services/api/tests/test_apply_brief.py`, a new file, with a
stubbed provider and synthetic briefs, postings and truth documents.

| Criterion | Proof |
|---|---|
| a brief round-trips through the store | test_a_brief_round_trips_through_put_document |
| an unknown key or a wrong type is refused on set | test_brief_set_refuses_an_unknown_key, test_brief_set_refuses_a_wrong_type |
| B1 a never-name in the letter refuses it | test_a_never_name_in_the_letter_refuses_it |
| B1 a never-name in an answer note refuses the set | test_a_never_name_in_an_answer_note_refuses_the_set |
| B1 matching is case-insensitive and on word boundaries | test_never_name_matches_case_insensitively_on_word_boundaries |
| B1 the never-name list reaches the prompt | test_the_never_name_list_reaches_the_prompt |
| B2 a letter over the stated word limit is refused | test_a_letter_over_the_stated_word_limit_is_refused |
| B2 an answer over the stated sentence limit is refused | test_an_answer_over_the_stated_sentence_limit_is_refused |
| B2 a stated paragraph count replaces three | test_a_two_paragraph_brief_accepts_two_paragraphs |
| B2 abbreviations and version numbers do not end a sentence | test_abbreviations_and_versions_do_not_split_sentences |
| B4 brief evidence verifies a candidate claim for its job only | test_brief_evidence_verifies_only_for_its_own_job |
| B4 guidance reaches the prompt and verifies employer claims | test_employer_guidance_reaches_the_prompt, test_employer_guidance_verifies_an_employer_claim |
| B5 each requirement kind is flagged from the posting | test_each_requirement_kind_is_flagged, parametrized over the four kinds |
| B5 a posting with none produces no flags | test_a_posting_without_requirements_has_no_flags |
| B6 a requirement question never reaches the model | test_a_work_authorization_question_is_not_sent_to_the_model |
| B7 an opinion question without a view becomes a placeholder | test_an_opinion_question_without_a_view_is_a_placeholder |
| B7 an interest question using "love" is not an opinion question | test_love_working_here_is_not_an_opinion_question |
| B7 a supplied view is sent and verifies | test_a_supplied_view_is_sent_and_counts_as_evidence |
| B8 the salary answer quotes the posted range and never reaches the model | test_the_salary_answer_quotes_the_posted_range, test_the_salary_question_is_not_sent_to_the_model |
| B8 no range and no number gives two honest lines | test_salary_without_range_or_number_says_so_and_asks |
| B9 the letter markdown ends with both sections | test_the_letter_markdown_ends_with_verify_and_next_action |
| B9 the next action picks a placeholder first, then a flag, then reading | test_next_action_priority, parametrized over the three cases |
| B9 there is exactly one next action | test_there_is_exactly_one_next_action |
| B10 the HTML carries no review section and no flag | test_the_letter_html_has_no_review_section_or_flag |
| the CLI loads the brief for its job | test_cli_cover_letter_uses_the_brief_for_its_job |

- [ ] every row above has a test that fails when its behavior is removed
- [ ] `config/application-brief.json` is classified never-in-git and
      gitignored before the example lands
      (`services/api/tests/test_classification_coverage.py`)
- [ ] the example brief is synthetic: no real company, client, person or
      number
- [ ] README.md "Application artifacts" describes the brief and names this
      spec's tests
- [ ] `just check` green

## Which lessons this tests, and which stay rules

| Lesson | Tested here | Rule only (prompt) |
|---|---|---|
| Anonymity: describe the work, not the client | B1 refuses the name | that the description is still accurate |
| Employer-aware shape: follow length and structure | B2 | structure beyond word, sentence and paragraph counts |
| Employer-aware shape: surface hard requirements | B5, B6 | requirements phrased outside the four kinds' patterns |
| Opinions stay drafts | B7 | |
| Compensation as an editable draft, never advice | B8 | |
| Respect stated limits | B2 | |
| Verify list and one next action | B9, B10 | that the next action really takes under two minutes |
| Fetch the employer's guidance | not built: operator pastes it | see Out of scope |

## Data and privacy

Briefs hold personal data (client names, the operator's views, a salary
number). They live only in the database (`data/**`, never-in-git). The
committed example is synthetic. A refusal under B1 names the offending
entry on stderr and in the local run log (`runtime/**`, never-in-git); it
never reaches a committed file. No API contract change: briefs are set
through the CLI only in this spec.

## Limitations

- **Guidance is pasted, not fetched.** The operator copies the company's
  guidance into the brief.
- **Requirement detection is keyword patterns.** A requirement worded
  outside them is not flagged. The flags are a prompt to look, not a
  guarantee nothing was missed.
- **Stated limits will refuse drafts the model overshoots.** The limits
  go into the prompt, but models miscount. A refusal costs a retry.
- **The sentence count is a heuristic.** A sentence starting with a
  lowercase word, or a quotation ending mid-sentence, can be miscounted.
- **No GUI for briefs.** Editing a brief in the web app needs new
  endpoints and a contract change; that is its own spec.

## Proof / origin

`normalize_cover_letter_text`, `validate_cover_letter` and
`classify_question` in `services/api/src/harrier/apply/` show the hard-coded
shape and the absent question kinds. The lessons come from an application
written by hand; no content from it appears here.
Reviewed for proportionality by `review-principal-architect` together
with spec 065: B3 moved there, B2 gained a defined sentence count, and B7
was narrowed so interest questions are not mistaken for opinions.

## Out of scope

Fetching `guidance_url`. It would add an outbound fetch to a host outside
the enrichment allow-list in
`services/api/src/harrier/screening/descriptions.py`, which deserves its
own review. The posting itself is already fetched and cached there. A GUI
for briefs. Changing the run manager's states.
