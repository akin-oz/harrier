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
- **Education section absent from the markdown** (a hand-edited
  markdown passed to `render_html`): the placeholder resolves to an
  empty string, matching today's behaviour for a missing section.
- **Operator's live document not migrated**: the first resume
  generation after deploy fails with the old-shape error naming the
  field. Nothing is written to the tracker or the artifact directory
  (the existing gate ordering).

## Acceptance criteria

- [ ] `parse_bundle` on the example bundle with
  `education: [{"degree": "MSc, X", "school": "U1"}, {"degree": "BSc,
  Y", "school": "U2"}]` yields entries in that order; a test in
  `test_resume.py` pins it.
- [ ] `parse_bundle` on `education: ["MSc, X", "U1"]` raises
  `ResumeBundleError` whose message contains `education must be a list
  of {degree, school} objects`; a test pins it.
- [ ] `parse_bundle` on an entry lacking `school` raises with
  `education[0] missing school`; a test pins it.
- [ ] The markdown resume for a two-entry bundle contains, under
  `## EDUCATION`, the lines `### MSc, X`, `U1`, `### BSc, Y`, `U2` in
  that order; a test pins it.
- [ ] `render_html` on that markdown produces HTML in which the MSc
  degree text appears before the bachelor's degree text, both schools
  appear, and the output contains no `{{` placeholder; a test pins it.
  The existing render test (`test_resume.py:175`) keeps passing.
- [ ] `render_html` with a template still containing
  `{{education_degree}}` raises `ValueError` naming that placeholder;
  a test pins it.
- [ ] `templates/resume-template.html` contains `{{education_html}}`
  and neither old placeholder.
- [ ] `config/resume-content.example.json` uses the new shape with two
  synthetic entries, and the fixture-driven tests load it.
- [ ] A degree string of `<b>x</b>` renders escaped in the HTML; a test
  pins it.
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
  `{{certifications_html}}`.
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
