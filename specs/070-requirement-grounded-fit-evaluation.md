---
spec: 070
title: The fit evaluation rates each posting requirement on the evidence that names it, and every gap becomes a question
status: accepted
approved: yes
milestone: M8
depends: [013, 066]
---

# Spec 070: The fit evaluation rates each posting requirement on the evidence that names it, and every gap becomes a question

## Problem

`harrier tailor` writes a fit evaluation beside every resume
(`<slug>.evaluation.md`, built by `evaluate_resume_fit` in
`services/api/src/harrier/resume/evaluation.py`). A run on a real
Senior/Principal posting on 2026-10-03 produced a report that said the
candidate was a strong fit for everything and had nothing to answer. Three
defects caused it. All three are in the code, not the data.

1. **Extraction reads the whole posting as requirements.** `_jd_units`
   splits every line and sentence. Any unit containing a dimension signal
   becomes a row. The company blurb above the requirements matched signals
   ("AI", "product", "scale") and became rows. A unit matching no signal is
   dropped, so "5+ years of experience in software development", "software
   design patterns and architectural distributed computing principles" and
   "Overlap with EU working hours (CET ±2 hours)" never appeared. A unit
   matching two dimensions became two rows.
2. **Evidence is attached per dimension, not per requirement.** A row's
   evidence is the dimension's fixed `evidence_refs`, whatever the
   requirement says. Two refs make "Strong evidence / high". So "Experience
   with AWS services and cloud architecture" was supported by a framework
   migration and a design system, and PostgreSQL by API contract work.
3. **Gaps never surface.** Section 4 lists only rows that are not strong,
   and there were none. Questions come only from a dimension's
   `candidate_question`, so section 6 said "None." PostgreSQL is absent
   from the CV and the report did not say so.

The report exists to say what the CV cannot back. It said the opposite.

## Scope

`evaluation.py` and its report only: how requirements are extracted, how
evidence is rated, and how gaps and questions are produced. The resume
itself (title, achievements, skills, dashes in the CV text) is spec 071.
Evaluation stays deterministic. No LLM call is added.

## Inputs

Unchanged: the resume content bundle, the posting text, the requested role.
One new committed module, `services/api/src/harrier/resume/vocabulary.py`,
holds public, persona-free vocabulary: section header phrases, a list of
common technologies with their aliases and families (for example AWS with
AWS Lambda and S3 as members), and the concept terms used by R5. It names
no candidate fact. The bundle's `technology_aliases` extend it for the
candidate's own technologies.

## Behavior

### Extraction

| # | Behavior |
|---|---|
| X1 | **Sections.** A line is a section header when it has at most five words, no sentence-ending punctuation, and matches a header phrase. Header phrases map to a section kind: requirement (Requirements, Qualifications, What you bring, Your profile, Must have, Nice to have, ...), responsibility (Tasks, Responsibilities, What you'll do, The role, ...), benefit (Benefits, What we offer, Perks, ...), company context (About us, Who we are, About <company>, ...). A header line is never an item. |
| X2 | **Classification.** Each remaining unit gets one class: `requirement`, `responsibility`, `company_context` or `benefit`, from the section it sits in. Text before the first recognized header is `company_context`. A unit anywhere that is an invitation or a legal notice ("let's talk", "if you enjoy", "join us", "equal opportunity employer") is `company_context`. |
| X3 | **No headers.** A posting with no recognized header classifies every unit as `requirement` except X2's invitation and legal patterns. This is the old behavior's coverage, and it is listed under Limitations. |
| X4 | **Matrix rows.** Only `requirement` and `responsibility` units become evidence matrix rows. `benefit` units are listed in their own report section. `company_context` units appear nowhere in the report. A unit is a row whether or not it matches a bundle dimension. |
| X5 | **Compound split.** A unit naming two or more technologies from the vocabulary, joined by `,`, `/`, `+`, `and` or `or`, becomes one sub-requirement per technology. Its text is the unit's lead phrase plus that technology ("Expert-level proficiency in PostgreSQL"), and it keeps the full unit as `source`. "React + Typescript / Next.js, Node.js, PostgreSQL" gives four rows. |
| X6 | **One row per requirement.** Rows are deduplicated on normalized text (case, whitespace and trailing punctuation ignored). When two units normalize to the same text, one row remains and it carries the union of their evidence. |
| X7 | **Importance.** `core` when the unit says must, required, expert or essential, or is among the first three items of the first requirement section. `nice-to-have` when it says nice to have, bonus, plus or preferred. Otherwise `important`. Responsibilities are `important` unless they say must. |

### Evidence rating

Statuses are `Direct`, `Partial`, `Adjacent` and `Unsupported`, best first.
A row takes the best status any single bullet earns. Every evidence entry
carries a one-line `reason`, written by code from the rule that fired.

| # | Behavior |
|---|---|
| R1 | **Direct, technology.** The requirement names technology T and the bullet names T (vocabulary or bundle alias, case-insensitive, on word boundaries). Reason: `names <T>`. |
| R2 | **Partial, family.** The requirement names a family (AWS) and the bullet names only a member of it (AWS Lambda). Reason: `names <member>, one part of <family>`. A member never earns Direct for its family. |
| R3 | **Partial, subset.** The requirement names two or more technologies or concept terms (after X5) and the bullet names some but not all. Reason: `names <covered>, not <missing>`. |
| R4 | **Direct, years.** A requirement of the form "N+ years" is Direct when the bundle's computed professional experience (`professional_experience_years` in `facts.py`) is at least N. Otherwise Unsupported. Reason: `<computed> of professional experience`. No bullet is cited. |
| R5 | **Direct, concept.** A requirement with no technology is Direct when the bullet contains one of the requirement's concept terms (the vocabulary terms or dimension signals found in the requirement text, for example `ci/cd`, `design system`, `distributed`). Reason: `shows <term>`. |
| R6 | **Adjacent.** The bullet is in the `evidence_refs` of a dimension the requirement matches but does not name the requirement's technology or concept term. Reason: `same area (<dimension>), does not name <term>`. A topical match is Adjacent at most, whatever the number of bullets. |
| R7 | **Unsupported.** No bullet earns any of the above. No evidence is cited. |
| R8 | **Confidence follows status.** Direct is `high`, Partial is `medium`, Adjacent and Unsupported are `low`. Nothing else changes confidence. |
| R9 | **Dimension kinds keep their caps.** `backend_ownership` caps a row at Partial unless a role lists the backend or full-stack competency. `absent_by_default` rows are Unsupported. These caps apply after R1 to R7. |

### Gaps and questions

| # | Behavior |
|---|---|
| G1 | **Section 4 is every row that is not Direct**, grouped by status (Partial, then Adjacent, then Unsupported), each with its requirement text. It is empty only when every row is Direct, and then it says so in one line. |
| G2 | **One question per gap row**, deduplicated, in section 4's order. Templates by what the row names: a technology T gives `Do you have hands-on <T> experience you can add to the CV?`; a family gives `Beyond <member>, which <family> services have you used in production?`; a concept gives `Which project shows <term>, and can it go on the CV?`; a requirement of one of spec 066's four kinds (`requirement_kind` in `services/api/src/harrier/apply/requirements.py`) gives `Can you confirm this requirement: "<text>"?`. A dimension's `candidate_question`, when present, replaces the template for rows of that dimension. |
| G3 | **Seniority gap.** When the requested role names a level (staff, principal, lead, head) that no bundle role title contains, one Unsupported row "`<Level>`-level scope" is added with the question `Which work shows <Level>-level scope, such as technical decisions across several teams?`. |
| G4 | **The compensation question stays** (posting mentions salary or compensation). |
| G5 | **The report never fills a gap.** No evaluation field writes a claim about the candidate that is not a bundle bullet quoted verbatim or a computed fact (R4). The tailoring action for a gap row is `Do not claim this. Ask the candidate (section 6).` |

### Report shape

The matrix gains a `Reason` column next to the evidence. Status values are
the four above. The old statuses (`Strong evidence`, `Partial evidence`,
`No evidence`, `Contradiction`) are removed; nothing outside
`evaluation.py`, its tests and the metadata sidecar reads them (checked
with grep on 2026-10-04). A new section lists benefits. Section numbering
otherwise stays.

## Acceptance criteria

Tests in `services/api/tests/test_resume_evaluation.py`, a new file, on the
synthetic bundle `config/resume-content.example.json`. The posting fixture
is `services/api/tests/fixtures/resume/weflow-senior-principal.txt`: the
real posting text with the founders' names removed (real company names are
allowed in job data, real people are not). It is public under the
classification config's complement rule.

| Criterion | Proof |
|---|---|
| X1 header lines are never rows | `test_section_headers_are_never_rows` |
| X2 classifier labels each kind | `test_classifier_labels_requirement_responsibility_benefit_and_context`, parametrized |
| X2 invitation and legal lines are company context anywhere | `test_invitation_and_eeo_lines_are_company_context`, `test_text_before_the_first_header_is_company_context` |
| X3 a posting without headers still yields requirement rows | `test_posting_without_headers_falls_back_to_requirements` |
| X5 a compound line splits per technology | `test_compound_technology_line_splits_into_sub_requirements`, `test_compound_split_keeps_a_remainder_that_names_a_concept` |
| X6 one row per requirement | `test_duplicate_requirement_lines_give_one_row` |
| X7 importance from wording and position | `test_importance_core_from_must_and_first_three` |
| R1 to R7 each status rule | `test_status_rules`, parametrized over one case per rule |
| R2 a family member is never Direct for the family | `test_aws_lambda_is_partial_for_aws` |
| R6 a topical match is Adjacent at most | `test_dimension_evidence_without_the_term_is_adjacent` |
| R8 confidence follows status | `test_confidence_follows_status` |
| R9 the kind caps hold | `test_backend_mention_is_capped_without_a_backend_role`, `test_absent_by_default_terms_are_never_covered`, `test_a_bullet_naming_an_absent_by_default_term_still_does_not_cover_it` |
| every evidence entry has a reason | `test_every_evidence_entry_has_a_one_line_reason` |
| G1 section 4 holds every non-Direct row | `test_section_four_lists_every_non_direct_row`, `test_section_four_says_so_when_everything_is_direct` |
| G2 one question per gap, by template | `test_one_question_per_gap_row` |
| G3 a principal role without principal titles adds a seniority gap | `test_principal_role_adds_seniority_gap`, `test_a_held_level_adds_no_seniority_gap` |
| G4 a compensation line naming nothing else is not a row | `test_compensation_lines_are_not_matrix_rows` |
| G5 gap rows carry no claim | `test_gap_rows_never_carry_a_claim` |
| X4 benefits are listed apart | `test_weflow_benefits_are_listed_apart` |
| Weflow: zero company-blurb rows | `test_weflow_has_no_company_context_rows` |
| Weflow: the three dropped requirements are rows | `test_weflow_keeps_years_design_patterns_and_working_hours` |
| Weflow: AWS and PostgreSQL are not Direct | `test_weflow_aws_and_postgresql_are_not_direct` |
| Weflow: section 4 non-empty, at least three questions | `test_weflow_report_has_gaps_and_at_least_three_questions` |

- [ ] the three existing fit-evaluation tests in
      `services/api/tests/test_resume.py` are updated to the new statuses,
      keeping what each pins (architecture supported, no invented game or
      AI experience, no assumed salary)
- [ ] each criterion's test fails when its behavior is removed
- [ ] `just check` green

## What the implementation decided

Recorded here so the spec and the code agree.

- **X2 carries forward inside a paragraph.** Once a sentence matches an
  invitation or legal pattern, the rest of that paragraph is company
  context too. The sentence after "equal opportunity employer" belongs to
  the notice, while the duties before "let's talk" on the same line stay
  responsibilities.
- **X5 keeps a remainder.** After a technology list is split out, the
  text left over stays as its own row when it still names a concept term.
  "Own the architecture of a React and TypeScript product" keeps its
  architecture part.
- **X7 core words** are exactly must, required, expert and essential. The
  word "requirement" is not one.
- **R9 caps, as built.** A term owned by an `absent_by_default` dimension
  is a term no bullet can cover, so a line naming React and "game" is
  Partial, not wholly Unsupported. A term owned by `backend_ownership`
  counts as Partial coverage when no role lists backend or full-stack. The
  `database` kind keeps its spec 013 rule: a database requirement with no
  API in it gets no Adjacent evidence from that dimension's refs.
- **G2 fallback.** A gap row naming no technology, no concept and no
  spec 066 kind gets `Which experience supports this requirement:
  "<text>"?`.
- **G4 and rows.** A compensation line that names no other term is not a
  matrix row; the compensation question covers it. A line naming salary
  and something else ("relational database experience") stays a row.
- **G5 for Partial rows.** The action is `Cite <refs> only for what each
  reason names.` followed by the gap action, because part of the row is
  backed.
- **`evaluate_resume_fit` takes `as_of`**, so R4 is testable on a fixed
  date. `harrier tailor` passes nothing and gets today.

## Data and privacy

The fixture is job data: company name allowed, the two founder names
removed, no candidate content. The evaluation of the operator's real
bundle stays under `data/**`. The before and after matrix for the real run
is shown to the operator in the pull request conversation only, not
committed.

## Limitations

- **Header detection is a phrase list.** A posting with unusual headers
  falls back to X3 and can again show blurb rows.
- **Technology detection is a vocabulary.** A technology missing from
  `vocabulary.py` and from the bundle aliases is not split out (X5) and is
  rated as a concept, usually Unsupported.
- **Concept matching is term matching.** A bullet that demonstrates a
  concept without using its term is rated Adjacent or Unsupported. This
  errs toward asking, which is the intended direction.
- **Questions are templates.** They are concrete but not tailored prose.

## Proof / origin

`services/api/src/harrier/resume/evaluation.py` as of commit d5cda0e
(`_jd_units`, `_status_for_evidence`, the `partial` filter). The failing
report is the operator's local
`data/resumes/*weflow*.evaluation.md`; none of its content appears here.

## Out of scope

The resume content and its wording (spec 071). An LLM requirement parser.
Feeding evaluation results into screening scores.
