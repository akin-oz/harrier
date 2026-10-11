---
spec: 130
title: A track's framing can write the CV's opening sentence
status: accepted
approved: yes
milestone: M9
depends: [071, 098, 099, 101]
---

# Spec 130: A track's framing can write the CV's opening sentence

## Problem

Every tailored CV opens with a sentence the code writes. Spec 071 O7 fixed
its wording: with `experience_statement` set, the profile begins

    <primary_identity> building user-facing web products with <skills>.

followed by the statement as its own sentence, then the framing's
`profile_summary`. `profile_lead` in `services/api/src/harrier/resume/plan.py`
builds it, and no field of the bundle changes the middle words.

That wording fits an industry search for web roles. It is wrong for an
academic track. A CV tailored for a doctoral call opens with a sentence of
the form "Software Engineer building user-facing web products with
<four web technologies>." A selection committee reads that first, before
education, and it describes a web developer applying for a job, not a
candidate for research.

Spec 099 gave each track its own framing so that a track presents the
shared facts in its own way. Spec 101 gave the academic kind its own CV
template, page count and voices. Neither reaches this sentence: the
framing can change the identity, the technologies and the summary, but
"building user-facing web products" is fixed for every track.

## Scope

- `services/api/src/harrier/resume/documents.py`: `FRAMING_CANDIDATE` gains
  `profile_lead`, so the split (spec 098) places it in the framing and the
  merge refuses it in the facts.
- `services/api/src/harrier/resume/content.py`: `ResumeBundle` gains
  `profile_lead`; the parser reads it and refuses it on the same rules as
  the other emitted candidate strings.
- `services/api/src/harrier/resume/plan.py`: `profile_lead()` returns the
  framing's sentence when one is set.
- `config/resume-framing.academic.example.json`: a synthetic
  `profile_lead`, so the example shows the field.
- Tests in `services/api/tests/test_resume.py` and
  `services/api/tests/test_track_framing.py`.

Nothing else changes. No migration: the field lives inside a framing
document's JSON, and a framing without it behaves as today.

## Behavior

### The field

A framing's `candidate` object may carry `profile_lead`, a string: the
CV's opening sentence, written by the operator for that track. It is
optional. It belongs to the framing, not the facts, because it is how a
track presents the candidate, not something true on every track.

### The profile with a lead

When `profile_lead` is set, the profile is, in order:

1. the lead, verbatim, with one period added when it ends without `.`,
   `!` or `?`;
2. the experience sentence: `experience_statement` as its own sentence,
   first letter capitalized, when set; otherwise
   `<N>+ years of professional experience.`, the label spec 071 derives;
3. the framing's `profile_summary`, when set.

The code adds no words of its own to the lead. `primary_identity` and
`positioning_technologies` are not inserted into it: the operator writes
the identity and any technologies into the sentence. Both fields keep
every other use they have today (the title line, ranking, the skills
order), and `positioning_technologies` stays required.

### The profile without a lead

Unchanged, byte for byte: spec 071 O7's wording when `experience_statement`
is set, and the derived sentence when it is not. A framing stored before
this change renders the same CV after it.

### Validation

At parse time, a set `profile_lead` is refused when it:

- is empty or only whitespace;
- spans more than one line;
- uses a dash as punctuation (spec 071's `dash_marks`);
- starts with a heading marker;
- states a year count larger than the computed one (`year_counts`, as
  spec 071 O7 refuses an `experience_statement`).

The checks the plan and the rendered markdown already run apply to the
whole profile, so they cover the lead with no new code: the hedge-word
check on everything before `profile_summary`, the calculated experience
length being present, forbidden phrases, and dash marks
(`validate_content_plan` in `plan.py`, the markdown checks in
`markdown.py`).

## Failure modes

- **A lead that claims more years than the record holds.** Refused at
  parse time, naming the field and both counts. `profile check` reports it,
  and `profile put` refuses to store it.
- **A lead in the facts document.** The merge refuses it as a key in the
  wrong document, as it refuses any framing key there (spec 098).
- **A lead that names an unverified skill or claim.** Not caught by the
  parser: the lead is operator prose, like `profile_summary`, and carries
  the same trust. The forbidden-phrase check on the rendered resume still
  applies (Honest limitations).
- **A lead with a hedge word.** Refused by the plan's existing hedge check,
  as a derived lead would be.

## Acceptance criteria

- [x] With `profile_lead` set, the profile is the lead, then the
      experience sentence, then `profile_summary`, and contains no
      "building user-facing web products"
      (`services/api/tests/test_resume.py::test_profile_lead_replaces_the_derived_sentence`).
- [x] A lead without closing punctuation gains one period; a lead ending
      in `.`, `!` or `?` is kept as written
      (`services/api/tests/test_resume.py::test_profile_lead_gains_one_period`).
- [x] With `profile_lead` set and no `experience_statement`, the derived
      `<N>+ years of professional experience.` sentence follows the lead
      (`services/api/tests/test_resume.py::test_profile_lead_without_a_statement_keeps_the_derived_years`).
- [x] Without `profile_lead`, the profile is identical to today's for a
      framing with and without `experience_statement`
      (`services/api/tests/test_resume.py::test_a_framing_without_a_lead_renders_as_before`).
- [x] An empty, multi-line, dash-punctuated or heading-marked lead, and one
      stating more years than computed, are each refused at parse time with
      a message naming `candidate.profile_lead`
      (`services/api/tests/test_resume.py::test_profile_lead_is_refused_on_the_emitted_string_rules`).
- [x] The split places `profile_lead` in the framing, and the merge refuses
      it in the facts
      (`services/api/tests/test_resume_documents.py::test_profile_lead_belongs_to_the_framing`).
- [x] Two tracks with different leads render different opening sentences
      over the same facts
      (`services/api/tests/test_track_framing.py::test_each_track_writes_its_own_profile_lead`).
- [x] `config/resume-framing.academic.example.json` carries a synthetic
      `profile_lead` and parses with `config/resume-facts.example.json`
      (`services/api/tests/test_track_framing.py::test_the_academic_example_framing_has_a_lead`).
- [x] `just check` passes, and no contract file changes.

## Honest limitations

- **The lead is trusted prose.** The truth gate reads bullets against the
  truth documents; it does not read `profile_summary`, and it will not read
  the lead. A lead can still overclaim within the rules above. This is the
  same trust `profile_summary` carries today.
- **It is per framing, not per kind.** An academic track with no lead still
  opens with the industry wording. A kind default was weighed and left out
  (Options weighed).
- **The AI pass does not write the lead.** It selects and orders bullets;
  the profile stays deterministic, as today.

## Options weighed

1. **A framing field (proposed).** The operator writes the sentence per
   track, and the code adds no persona text, which keeps the engine
   persona-free as `profile_lead`'s docstring says it is.
2. **A per-kind phrase in `KindRules`.** The academic kind would carry its
   own middle words, such as "with experience in". The code would then
   write candidate prose for each kind, and an academic track whose
   candidate is not a software engineer would still get a phrase chosen in
   code.
3. **Drop the derived sentence for the academic kind.** The profile would
   start with the experience sentence. It leaves the CV without an opening
   claim and still gives the operator no say.

## Decisions recorded (Akin, 2026-10-11)

Approved as written, so each open decision keeps the behavior the spec
states:

1. **A framing field only.** No per-kind default lead.
2. **Skills are not appended.** The lead is verbatim; the operator names
   technologies in it.
3. **The experience sentence always follows the lead.**

## Out of scope

- **A per-kind default lead.** Option 2 above, left for a later spec if a
  framing with no lead turns out to need one.
- **Truth-gating operator prose.** Reading `profile_summary` and the lead
  against the truth documents is its own change; today neither is read.
- **The cover letter's opening.** Letters have their own voices (spec 101)
  and are not built from `profile_lead`.
- **The CV title line and section order.** Unchanged; the academic
  template already orders sections for its kind (spec 101).

## Data and privacy

The lead is personal prose and lives in the framing document in the
profile store, never in git (ADR-008). The example file and every test
fixture use synthetic values only.

## Migration

None. The field is optional inside a stored framing's JSON.

## Proof / origin

- `profile_lead` in `services/api/src/harrier/resume/plan.py` writes the
  industry sentence for every track, the academic kind's template
  (spec 101) included.
- Spec 071 O7 fixed the wording; this spec gives the framing a way to
  replace it.

## Amendment (2026-10-11, during implementation)

- **A lead that is not a string is refused** with "candidate.profile_lead
  must be a string", beside the empty-lead refusal. The spec listed the
  string rules only; a number or a list in that field would otherwise have
  been read as no lead
  (`services/api/tests/test_resume.py::test_profile_lead_is_refused_on_the_emitted_string_rules`).
- **Each criterion was checked by mutating the source:** ignoring the
  lead, always adding a period, dropping the derived years after a lead,
  leaving `profile_lead` out of the framing table, accepting an empty
  lead, not counting the lead's years, and allowing a heading marker each
  failed a test above.
