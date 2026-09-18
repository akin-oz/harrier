---
spec: 062
title: Bundle strings the resume markdown emits are single lines that cannot forge structure
status: accepted
approved: yes
milestone: M8
depends: [013, 059]
---

# Spec 062: Bundle strings the resume markdown emits are single lines that cannot forge structure

## Problem

The tailored resume is assembled as line-oriented markdown
(`services/api/src/harrier/resume/markdown.py`, `build_markdown`) and
then parsed back by the HTML renderer
(`services/api/src/harrier/resume/htmlrender.py`, `_extract_section`,
`_parse_experience`, `_parse_education`). The renderer finds a section
with `lines.index("## NAME")`, which returns the first match, and ends
it at the next line starting with `## `. Any bundle string that carries
a line boundary, or that starts an unmarked line with `#`, therefore
edits the document's structure instead of its text.

Spec 059 closed this for `education` only, and named the rest as
belonging to the bundle validator that spec 013 owns. This spec is that
work. It amends spec 013's validator and one clause of spec 059.

The worst case breaks a product invariant. A `profile_summary` of
`Fine.\n## SELECTED ACHIEVEMENTS\n- INVENTED claim never in truth`
passes `parse_bundle`, `build_content_plan`, and `build_markdown`, and
`render_html` then shows `INVENTED claim never in truth` as the only
selected achievement. The forged section sits above the real one, so
the real, truth-checked bullets are never read. No bullet ID is
involved, so `resolve_bullets` and the spec 034 truth gate never see
the line. "Verified content only" holds for bullets and fails for every
other string.

### What was run

One script, in memory, no database and no network, on branch
`feat-059-resume-education-entries` at `3dab67e`, with
`HARRIER_DATA_DIR` pointed at a scratch directory because the `harrier`
container was running. For each case it deep-copies
`config/resume-content.example.json`, mutates one field, and calls
`parse_bundle`, `build_content_plan(bundle, "", "Senior Frontend
Engineer", date(2026, 8, 1))`, `build_markdown`, and `render_html` with
a probe template directory holding one tagged slot per placeholder.
Each rendered slot is compared with the unmutated baseline. Truth
sources are the pool texts joined by line feeds, as the `sources`
fixture in `services/api/tests/test_resume.py` builds them.

A first version of the script checked only that a marker string
appeared somewhere in the HTML body. That proves nothing for contact
fields, which the renderer copies from the bundle directly, so it was
replaced by the slot comparison before any result below was recorded.
A control case (`Real Cert ## TECHNICAL SKILLS COBOL`, plain spaces)
changes only the certification text, which confirms the comparison
separates a structural rewrite from an odd value.

### Results: a line feed inside the value

Accepted by every stage, structure rewritten:

- `certifications[0]` of `Real Cert\n## TECHNICAL SKILLS\nCOBOL`: the
  skills slot renders `COBOL`.
- `profile_summary`: the forged achievement above.
- `candidate.location`, `candidate.email`, `candidate.linkedin`, each
  ending in `\n## PROFILE\nINJECTED profile`: the profile slot renders
  the injected text and the real profile is dropped.
- `roles[0].title` of `Senior Frontend Engineer\n- INJECTED bullet`:
  the period slot renders `- INJECTED bullet (Freelance)`, the real
  period is lost, and the role's bullets vanish.
- `roles[0].employment_type` with a line feed: the title renders as
  `Senior Frontend Engineer (Freelance` and every later role is
  dropped from the experience slot.
- An `all_skills` entry with a line feed (renamed consistently across
  `verified_skills`, `positioning_technologies`, role technologies,
  and aliases): the profile is cut mid-sentence, the skills line
  renders one skill, and the rest lands in certifications.
- A `positioning_technologies` entry of `Vue 3\n## SELECTED
  ACHIEVEMENTS\n- INVENTED via skill`: a second route to a forged
  achievement.

Accepted by `parse_bundle`, stopped later by an unrelated check:

- `roles[0].organization`: `render_html` crashes with `ValueError: not
  enough values to unpack (expected 2, got 1)`, after the markdown was
  built. An unnamed crash, not a refusal.
- `candidate.name` and `candidate.primary_identity`: `build_markdown`
  raises `rendered title differs from the grounded content plan`. This
  is a coincidence, not a guard. A `name` of `Deniz Örnek\nSenior
  Frontend Engineer`, whose second line equals the planned title, is
  accepted by every stage.
- `bullet_pool` text with an interior line feed: `UnverifiedClaimError`,
  because `TruthSources.contains` matches within one truth line. A
  trailing line feed is accepted, since `contains` strips the fragment,
  and renders identically to the baseline.
- `roles[0].period.start`: `ResumeContentError: invalid ISO date`. The
  emitted period is a formatted date, never the bundle string.

Not emitted by the markdown at all:

- `candidate.phone` with a line feed renders every slot identically to
  the baseline. The renderer copies it from the bundle and escapes it.

### Results: a heading marker at the start of the value

- `certifications` of `["## TECHNICAL SKILLS", "COBOL"]`: the skills
  slot renders `COBOL` and the certifications slot is empty.
- `candidate.primary_identity` of `## PROFILE`: the headline renders
  `## PROFILE` and the profile slot renders the contact line. With
  `## Senior Frontend Engineer` the profile slot is empty, because the
  profile line itself now ends the section.
- The first-ranked skill renamed to `## TypeScript`: the skills slot is
  empty. Which skill ranks first depends on the job description, so
  any `all_skills` entry can take that position.
- `certifications[0]` of `### odd cert` renders the literal text. Not
  structural today, since certifications have no entry parser.
- `candidate.location` of `## PROFILE`: every slot identical to the
  baseline. The contact line sits above every section and the section
  lookup needs an exact match.
- `   ## TECHNICAL SKILLS` (leading spaces) as a certification is not a
  heading to the renderer, which does not strip. It is one to
  `_section_is_populated` in `markdown.py`, which does.

### Results: the organization and title separator

`build_markdown` writes `### <organization> <sep> <title>`, where
`<sep>` is space, U+2014, space, and `_parse_experience` splits on the
first `<sep>`. An organization of `Acme <sep> Talent` renders the
company as `Acme` and the title as `Talent <sep> Senior Frontend
Engineer (Freelance)`.

### Results: spec 059's guard is incomplete

Both parsers use `str.splitlines()`, which also breaks on U+000B,
U+000C, U+001C, U+001D, U+001E, U+0085, U+2028, and U+2029. Spec 059
refuses only carriage return and line feed. Executed:

- each of the eight characters in place of the line feeds in the
  certification payload renders `COBOL` as the skills line;
- an education `degree` of `MSc`, U+2028, `## CERTIFICATIONS`, U+2028,
  `INJECTED cert` passes `parse_bundle` on the branch that carries the
  spec 059 guard, drops the school, and renders `INJECTED cert` and
  `U1` as certifications;
- an education `school` of `U1`, U+2028, `### INJECTED degree` passes
  and renders a second degree.

## Scope

- `services/api/src/harrier/resume/content.py`: `parse_bundle` and its
  helpers. New named errors only.
- `services/api/tests/test_resume.py`: the tests named below.
- The operator's local `resume_data` document, only if it violates the
  rules: a local data operation through `put_document`, never in git.

No change to `markdown.py`, `htmlrender.py`, the templates, the
contract, the tracker schema, or the web app.
`config/resume-content.example.json` already satisfies every rule (it
is the baseline of the reproduction) and is not edited.

## Behavior

All rules run in `parse_bundle`, on the value as written in the bundle,
before any trimming. Every violation is collected and reported in the
one existing `ResumeBundleError`, `invalid resume content bundle:
<problems joined by "; ">`, alongside the existing problems.

**Rule 1: single line.** A value is a single line when it contains none
of the ten characters at which `str.splitlines()` breaks: U+000A,
U+000D, U+000B, U+000C, U+001C, U+001D, U+001E, U+0085, U+2028, U+2029.
A trailing one counts. The error is `<path> must be a single line`. It
applies to every bundle string `build_markdown` emits:

- `candidate.name`, `candidate.location`, `candidate.email`,
  `candidate.linkedin`, `candidate.primary_identity`
- `candidate.positioning_technologies[<i>]`
- `profile_summary`
- `roles[<i>].organization`, `roles[<i>].title`,
  `roles[<i>].employment_type`
- `all_skills[<i>]`
- `bullet_pool[<id>]`
- `certifications[<i>]`
- `education[<i>] degree` and `education[<i>] school`: spec 059's
  message, unchanged in text, now covering all ten characters. This is
  the amendment to spec 059.

`verified_skills` needs no rule of its own: an entry absent from
`all_skills` is already refused, and one present is checked there.

**Rule 2: no heading marker at an unmarked line start.** Where a value
begins a markdown line with no writer marker in front of it, its first
non-space character must not be `#`. The error is `<path> must not
start with a heading marker`, the wording and the leading-space
handling of spec 059's `school` rule. It applies to:

- `candidate.primary_identity` (begins the title line and the profile
  line)
- `candidate.location` (begins the contact line)
- `all_skills[<i>]` (any entry can rank first on the skills line)
- `certifications[<i>]`

Values that always sit behind a writer marker or mid-line keep their
freedom to start with `#`, as `degree` does in spec 059: `name` (behind
`# `), `organization` (behind `### `), bullet texts (behind `- `),
`title`, `employment_type`, `email`, `linkedin`, `profile_summary`,
and `positioning_technologies` entries.

**Rule 3: the organization cannot contain the separator.**
`roles[<i>].organization` must not contain space, U+2014, space. The
error is `roles[<i>].organization must not contain the title
separator`. `title` may contain it: the parser splits on the first
occurrence, so with a clean organization the split is exact.

A bundle that breaks no rule parses to the same `ResumeBundle` as
today, and its markdown, HTML, and PDF are byte-identical.

## Failure modes

- **Any rule broken**: `load_bundle` raises before the content plan, any
  LLM call, any file write, the PDF render, and the tracker update.
  `harrier tailor` reports the message through its existing
  `ResumeBundleError` handler (`services/api/src/harrier_cli/main.py`).
- **Several fields broken at once**: one error names all of them, so
  the operator fixes the document in one edit.
- **The operator's live document breaks a rule after deploy**: resume
  generation and offer evaluation (`offers/evaluate.py`,
  `build_system_prompt`) fail with the named error until the document
  is fixed. While it is refused, `artifacts.py` `_resume_paths` treats
  the bundle as not configured and reports existing resume artifacts
  as absent. That is today's behaviour for any invalid bundle,
  including spec 059's old-shape refusal, and is not changed here.
- **Saving a bad bundle**: `put_document` does not validate bundle
  content today and still will not. The refusal arrives at the next
  load, not at save.
- **A value whose only line boundary is trailing** (a stray newline from
  an editor): refused, although it renders harmlessly today. One rule
  with no exceptions is easier to hold than a rule about where in the
  string the character sits.
- **Non-string entries** in `certifications`, `all_skills`, or
  `bullet_pool`: still dropped silently by `_str_tuple` and
  `_str_dict`, as today. Not changed here.
- **A hand-edited markdown passed to `render_html`**: unchanged. The
  rules guard the bundle, not the parser.

## Acceptance criteria

Every test named here is in `services/api/tests/test_resume.py`. The
spec was approved describing them, because
`services/api/tests/test_spec_structure.py::test_every_test_a_spec_names_actually_exists`
fails on a test symbol that is not yet defined; the implementing change
named them. Every test mutates the example bundle and calls
`parse_bundle`, and each assertion checks the `ResumeBundleError`
message for the named path.

- [ ] A line feed in each Rule 1 field raises with `<path> must be a
  single line`, parametrized over every path in the Rule 1 list:
  `test_line_break_in_any_emitted_bundle_string_is_refused_by_name`.
- [ ] Each of the ten boundary characters in `certifications[0]` raises
  with `certifications[0] must be a single line`:
  `test_every_line_boundary_character_is_refused`.
- [ ] The ten characters are exactly the code points at which
  `str.splitlines()` breaks, checked against every code point rather
  than recalled (added during implementation: the list is the rule, and
  a character missing from it is a way back in):
  `test_the_refused_boundaries_are_every_character_splitlines_breaks_on`.
- [ ] A value ending in a line feed, with nothing after it, raises, for
  `candidate.name` and `bullet_pool[r1_b1]`:
  `test_trailing_line_break_is_refused`.
- [ ] U+2028 in an education `degree` and in a `school` raises with
  spec 059's `education[0] <field> must be a single line`:
  `test_education_line_boundary_beyond_cr_lf_is_refused`.
- [ ] The reproduction's `profile_summary` payload raises with
  `profile_summary must be a single line`:
  `test_profile_summary_cannot_inject_an_unverified_achievement`.
- [ ] `## PROFILE`, `### x`, and `   ## x` raise with `<path> must not
  start with a heading marker` for `candidate.primary_identity`,
  `candidate.location`, `all_skills[0]`, and `certifications[0]`:
  `test_heading_marker_at_an_unmarked_line_start_is_refused`.
- [ ] A `name`, an `organization`, and a pool bullet starting with `#`
  parse, and `render_html` shows each once with the role count and the
  section contents otherwise unchanged:
  `test_values_behind_a_writer_marker_may_start_with_hash`.
- [ ] An organization containing the separator raises with
  `roles[0].organization must not contain the title separator`:
  `test_organization_containing_the_title_separator_is_refused`.
- [ ] A `title` containing the separator parses and renders with the
  company intact and the full title:
  `test_title_containing_the_separator_stays_one_role`.
- [ ] A bundle breaking Rule 1 in two fields and Rule 2 in a third
  raises once, and the message names all three:
  `test_every_line_problem_is_reported_in_one_error`.
- [ ] `run_tailor` on a stored bundle with a line feed in a
  certification raises `ResumeBundleError`, writes nothing under the
  output directory, and leaves the tracker status unchanged (uses the
  existing `tailor_env` fixture):
  `test_bundle_with_a_line_break_fails_tailor_before_any_file_is_written`.
- [ ] The unmodified example bundle still parses. The `bundle` fixture
  proves it, so every fixture-driven test fails otherwise.
- [ ] The diff touches only `content.py`, `test_resume.py`, and this
  spec, where it names the tests.
- [ ] `just gate` passes.

## Proof / origin

- Writer: `services/api/src/harrier/resume/markdown.py`,
  `build_markdown`, and `plan.py` `build_profile` and
  `build_presentation_title`, which decide what reaches each line.
- Parser: `services/api/src/harrier/resume/htmlrender.py`.
- Precedent and wording: spec 059, the "Value that would rewrite the
  markdown's structure" failure mode and its Out of scope note.
- Invariant at stake: "Verified content only" (product invariants),
  spec 034.
- Old system: `~/job-hunt-local/scripts/tailor_resume.py` held this
  content as code constants, so no value could carry a line break. The
  exposure arrived when spec 013 moved the content into data.

## Out of scope

- Hardening the renderer or adding a structural check to
  `validate_rendered_markdown` (for example comparing the rendered
  sections with the plan). That would catch a future emitted field this
  list misses. It is a second layer and its own spec.
- `candidate.phone`, which the markdown never emits (executed: no slot
  changes).
- Role periods. The emitted label is a formatted date, and a bad value
  is already refused by `parse_iso_date`. That it fails at plan time
  with `ResumeContentError` rather than at load is a separate matter.
- Strings the markdown never emits: `forbidden_phrases`,
  `evidence_groups`, `technology_aliases`, `target_signal_weights`,
  evaluation dimensions, role `technologies` and `competencies`.
- A value starting with `- ` on an unmarked line (a certification
  rendered with a literal dash). Cosmetic, not structural.
- Silent dropping of non-string list entries.
- Validating the bundle at `put_document` time.
- How `artifacts.py` reports artifacts while the bundle is invalid.
- The cover letter, answers, and outreach paths, which do not go
  through this markdown.
- Any contract, tracker, or web change.

## Migration

The operator's local `resume_data` document must satisfy the three
rules before this lands, or the first generation after deploy fails
with an error naming each offending field. It was not inspected for
this spec: the container was running and the document is personal
data. The likely offenders are an organization written with the
separator and a trailing newline left by an editor. The fix is a local
edit through `put_document`, never in git. Nothing changes for a bundle
that already complies, and the example bundle in git needs no edit.
