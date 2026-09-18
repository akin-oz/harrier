---
spec: 059
title: The resume PDF renders every education entry in bundle order
status: proposed
approved: yes
milestone: M8
depends: [013]
---

# Spec 059: The resume PDF renders every education entry in bundle order

## Problem

The resume bundle's `education` field is a flat list of lines, and the
HTML renderer (`services/api/src/harrier/resume/htmlrender.py`) copies
only the first two of them into the template's `{{education_degree}}`
and `{{education_school}}` placeholders. That was a faithful port of
the old script (`~/job-hunt-local/scripts/tailor_resume.py`, lines
1209 to 1341), which hardcoded exactly one degree.

The operator now has two degrees to show: a bachelor's and an MSc in
progress. The markdown resume lists all four lines, but the PDF, which
is the artifact recruiters receive, silently drops everything after the
second line. Reordering the list to put the MSc first would drop the
bachelor's instead. There is no way to get both onto the PDF, and the
drop is silent: no error, no warning, a clean one-page PDF with half
the education missing.

A flat line list also cannot say where one degree ends and the next
begins, so the renderer could not lay two degrees out as two entries
even if it kept every line.

## Scope

- `services/api/src/harrier/resume/content.py`: the bundle's
  `education` shape and its validation errors.
- `services/api/src/harrier/resume/markdown.py`: the `## EDUCATION`
  section of the markdown resume.
- `services/api/src/harrier/resume/htmlrender.py` and
  `templates/resume-template.html` (plus `.css` if spacing needs it):
  the education block of the PDF.
- `config/resume-content.example.json`: the synthetic example bundle.
- `services/api/tests/test_resume.py`: the tests named below.
- The operator's local `resume_data` document: a one-time local data
  operation at landing, never in git (same pattern as spec 013).

No contract, tracker schema, or web change: nothing outside
`harrier.resume` reads `education` (`grep -rn education
services/api/src/harrier` returns only the three resume modules).

## Behavior

**Bundle shape.** `education` becomes an ordered list of entries. Each
entry is an object with two required non-empty strings, `degree` and
`school`. Anything a degree needs beyond that (part-time, expected
year, honours) goes into the `degree` string, as the period already
does today. Order in the list is display order; the bundle author puts
the newest degree first. The renderer never sorts.

Validation, in `parse_bundle`, rejects with a named error (joined into
the existing `invalid resume content bundle:` message):

- `education` is a list of strings (the old shape): `education must be
  a list of {degree, school} objects`.
- an entry missing `degree` or `school`, or with an empty one:
  `education[<i>] missing <field>`.
- a `degree` or `school` containing a carriage return or line feed:
  `education[<i>] <field> must be a single line`.
- a `school` whose first non-space character is `#`:
  `education[<i>] school must not start with a heading marker`.
- an empty list is valid and renders an empty education block, as an
  empty list does today.

**Markdown resume.** Under `## EDUCATION`, each entry is written as a
`### <degree>` heading line followed by one `<school>` line, entries in
bundle order. This mirrors how `## EXPERIENCE` already marks entries
with `### ` headings, so the same line-based section parser serves
both.

**PDF.** The template's two placeholders are replaced by one,
`{{education_html}}`. The renderer parses the markdown education
section into entries by its `### ` headings and emits, per entry, a
block with the degree on one line and the school on the next, in
markdown order, every value HTML-escaped. With two entries the footer
column shows the MSc block above the bachelor's block, visibly two
entries rather than four undifferentiated lines. An `## EDUCATION`
section with no entries emits an empty string, and the `Education`
label still renders, as it does today for an empty list.

The unresolved-placeholder check is unchanged: a template still
carrying `{{education_degree}}` fails the render with the existing
`unresolved resume template placeholders` error, which is how a stale
template copy is caught.

**Page gate.** Unchanged. The extra lines sit in the footer block,
which already carries `break-inside: avoid`. If a bundle's education
pushes the PDF to two pages, `validate_rendered_pdf` fails the
generation with its existing page-count error, and the operator
shortens the degree strings. The gate stays the proof that the artifact
is one page; this spec does not loosen it.

## Failure modes

- **Old-shape bundle after the change**: generation fails at bundle
  load with the named error above, before any LLM call or PDF render.
  It does not fall back to treating the lines as one entry, because a
  silent reinterpretation is the failure this spec exists to remove.
- **Entry with a missing school**: rejected at load, same path. The
  renderer never sees a half entry.
- **Degree string containing `### ` or HTML**: the markdown writer
  emits it verbatim after the `### ` marker (the parser only checks the
  line prefix), and the HTML renderer escapes it. No injection into the
  template.
- **Value that would rewrite the markdown's structure** (amended after
  a review finding on PR #68, reproduced before fixing): the markdown
  resume is line-oriented and the renderer re-parses it, so a line
  break in `degree` closed the education section, dropped the school,
  and rendered the rest as a certification; a `school` starting with
  `### ` was read back as a second degree, and one starting with `## `
  ended the section. Both are refused at bundle load with the named
  errors above. The degree keeps its freedom to start with `#`, because
  it always sits behind the writer's own `### ` marker.
- **Education section absent from the markdown** (a hand-edited
  markdown passed to `render_html`): the placeholder resolves to an
  empty string, matching today's behaviour for a missing section.
- **Operator's live document not migrated**: the first resume
  generation after deploy fails with the old-shape error naming the
  field. Nothing is written to the tracker or the artifact directory
  (the existing gate ordering).

## Acceptance criteria

Every test named here is in `services/api/tests/test_resume.py`.

- [ ] `parse_bundle` on the example bundle with
  `education: [{"degree": "MSc, X", "school": "U1"}, {"degree": "BSc,
  Y", "school": "U2"}]` yields entries in that order:
  `test_education_entries_keep_bundle_order`.
- [ ] `parse_bundle` on `education: ["MSc, X", "U1"]` raises
  `ResumeBundleError` whose message contains `education must be a list
  of {degree, school} objects`:
  `test_education_flat_line_list_is_refused_by_name`.
- [ ] `parse_bundle` on an entry lacking `school` raises with
  `education[0] missing school`:
  `test_education_entry_missing_school_is_refused`. A blank `degree`
  raises with `education[1] missing degree`:
  `test_education_entry_with_empty_degree_is_refused`.
- [ ] A carriage return or line feed in `degree` or `school` raises
  with `education[0] <field> must be a single line`:
  `test_education_line_break_in_either_field_is_refused`.
- [ ] A `school` of `### Not A Degree`, `## CERTIFICATIONS`, or the
  same behind leading spaces raises with `education[0] school must not
  start with a heading marker`:
  `test_education_school_that_looks_like_a_heading_is_refused`.
- [ ] A `degree` of `### odd` renders as one entry with its school:
  `test_degree_starting_with_heading_marker_stays_one_entry`.
- [ ] An empty `education` list is valid, the markdown section is
  empty, and the PDF keeps the `Education` label with no entry blocks:
  `test_empty_education_is_valid_and_renders_an_empty_block`.
- [ ] The markdown resume for a two-entry bundle contains, under
  `## EDUCATION`, the lines `### MSc, X`, `U1`, `### BSc, Y`, `U2` in
  that order:
  `test_markdown_writes_each_degree_as_a_heading_then_its_school`.
- [ ] `render_html` on that markdown produces HTML in which the MSc
  degree text appears before the bachelor's degree text, both schools
  appear, and the output contains no `{{` placeholder:
  `test_html_renders_every_degree_newest_first`. The existing render
  test `test_html_header_uses_grounded_markdown_title` keeps passing.
- [ ] `render_html` with a template still containing
  `{{education_degree}}` raises `ValueError` naming that placeholder:
  `test_stale_template_with_old_education_placeholders_fails_the_render`.
- [ ] `templates/resume-template.html` contains `{{education_html}}`
  and neither old placeholder. Without the new placeholder
  `test_html_renders_every_degree_newest_first` finds no entry blocks
  and fails.
- [ ] `config/resume-content.example.json` uses the new shape with two
  synthetic entries. The `bundle` fixture parses that file, so every
  fixture-driven test fails on the old shape.
- [ ] A degree string of `<b>x</b>` renders escaped in the HTML:
  `test_html_escapes_degree_text`.
- [ ] `just gate` passes.

## Proof / origin

- Old behaviour: `~/job-hunt-local/scripts/tailor_resume.py` lines
  1209 to 1212 (hardcoded two lines) and 1340 to 1341 (first two lines
  only), ported unchanged by spec 013 into `htmlrender.py`.
- Template: `templates/resume-template.html` lines 56 to 61.
- Page gate: `services/api/src/harrier/resume/pdf.py`,
  `validate_rendered_pdf`.
- Content-out-of-code rule: ADR-008, ADR-009, spec 013.

## Out of scope

- Sorting or date-parsing education entries. Order is authored in the
  bundle.
- A `period`, `location`, or `notes` field on entries. The degree
  string carries qualifiers, as it does today.
- Any change to certifications, which keep their flat list and
  `{{certifications_html}}`. That includes the single-line rule: a
  line break in a certification still rewrites the markdown (executed
  on this branch: a certification of `Real Cert\n## TECHNICAL
  SKILLS\nCOBOL` renders `COBOL` as the skills line). The same holds
  for every other bundle string the markdown writer emits. It predates
  this spec and belongs to the bundle validator that spec 013 owns.
- Changing the footer layout beyond what two entries need. The
  one-page gate is not relaxed.
- Truth-source validation of education lines. Education is not
  validated against the truth document today and this spec does not
  start that; it belongs in its own spec if wanted.
- The cover letter template, which has no education block.
- Any web or contract change.

## Migration

The operator's local `resume_data` document must be converted from the
four-line list to two entries, MSc first, in the same landing (a local
data operation through `put_document`, never in git). Until it is
converted, resume generation fails with the old-shape error. Nothing
else changes for existing users: the example bundle in git is updated
in the same change, and the truth source needs no edit.
