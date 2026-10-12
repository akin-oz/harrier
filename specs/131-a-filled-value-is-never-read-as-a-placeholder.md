---
spec: 131
title: A filled template value is never read as a placeholder
status: proposed
approved: no
milestone: M9
depends: [013, 014, 062, 130]
---

# Spec 131: A filled template value is never read as a placeholder

## Problem

The resume and the cover letter are HTML templates with `{{name}}`
placeholders (specs 013 and 014). Both renderers fill them one name at a
time with `str.replace`, over the whole document each time:

- `services/api/src/harrier/resume/htmlrender.py`, `render_html`, the loop
  over `replacements` and the four `*_html` replacements after it.
- `services/api/src/harrier/apply/letters.py`, `render_cover_letter_html`,
  the loop over `replacements`.

`html.escape` leaves `{` and `}` alone. So a value that contains a
placeholder's name is filled again by every replacement that runs after
it. Then the unresolved-placeholder check scans the filled output, so a
value that names any other placeholder fails the render.

Reproduced on the synthetic bundle (`config/resume-facts.example.json` with
`config/resume-framing.example.json`) and the `db` fixture in
`services/api/tests/test_apply.py`, on `main` at `4fa400b`:

| Input | Result |
| --- | --- |
| `profile_summary` = `Builds {{experience_html}} things.` | renders, 6 role articles instead of 3 |
| `candidate.profile_lead` = `Researcher {{experience_html}}` | renders, 6 role articles instead of 3 |
| `certifications[0]` = `{{education_html}}` | renders, 4 education items instead of 2 |
| `candidate.name` = `A {{profile}}` | renders, the whole profile inside `<h1>` and `<title>` |
| `profile_summary` = `x {{nope}}` | `ValueError: unresolved resume template placeholders: nope` |
| `education[0].school` = `U {{name}}` | `ValueError: unresolved resume template placeholders: name` |
| cover letter job title `Engineer {{full_version_html}}` | renders, the letter body twice (9 paragraphs instead of 3) |
| cover letter body ending `See {{name}}` | `ValueError: unresolved cover letter template placeholders: name` |

The duplicated resume still passes the PDF gate when it fits the page
limit. The review of PR #282 (spec 130) found the `profile_lead` case;
`profile_summary` has had it since spec 013.

Who it hurts:

- The operator. Their own prose can silently duplicate a section of a
  recruiter-facing CV, or fail a render with a message that blames the
  template.
- Anyone who writes text that reaches a template. The cover letter's
  headline is the job title from the tracker row, which comes from a
  scraped posting. The letter body is model output. Neither is operator
  text, and neither passes through the bundle parser.

The finding names two fields. The shape is wider: every value either
renderer fills is exposed, including values no parse-time check sees
(the job title, the model-written letter, and skills confirmed in a job's
brief, which join `all_skills` after parsing in
`harrier.resume.plan.with_confirmed_skills`).

## Scope

- `services/api/src/harrier/resume/htmlrender.py`: one fill function, used
  by `render_html`.
- `services/api/src/harrier/apply/letters.py`: `render_cover_letter_html`
  uses the same function.
- Tests in `services/api/tests/test_resume.py`,
  `services/api/tests/test_apply.py` and `services/api/tests/test_pdf_gate.py`.

Nothing else changes. No template, no bundle rule, no migration.

## Behavior

### One pass over the template

Each renderer fills its template in one pass. The renderer finds every
placeholder in the template text, as written in `templates/` with the
stylesheet already inlined, and replaces each occurrence with its value.
The output is never scanned for placeholders again. A placeholder is
`{{` followed by one or more of `a-z A-Z 0-9 _` and then `}}`, the pattern
both renderers and the PDF gate use today.

A placeholder the template names twice (`name`, `email`) is filled at
both places.

### A value's braces are printed, not parsed

Every value is inserted with each `{` written as `&#123;` and each `}`
written as `&#125;`. This applies to escaped text values and to the
HTML blocks the renderers build (`experience_html`, `education_html`,
`selected_achievements_html`, `certifications_html`,
`full_version_html`). The browser shows the same braces the value
contained. The rendered HTML contains a literal `{{name}}` only where the
template put one, and by the time a render returns there are none.

So a bundle string, a job title or a letter body that contains
`{{experience_html}}` appears on the page as the text
`{{experience_html}}`, once, where that value belongs. No section is
duplicated, and no render fails because of it.

The inlined stylesheet is template text, not a value. Its braces are
left as written.

### What still fails

A placeholder in the template with no value fails the render, as today,
with the same message: `unresolved resume template placeholders: <names>`
or `unresolved cover letter template placeholders: <names>`, names sorted
and listed once. The message names placeholders only, never a value.

The PDF gate (`services/api/src/harrier/resume/pdf.py`,
`validate_rendered_pdf`) is unchanged. It still refuses HTML that contains
`{{name}}`. After this change only a template placeholder can produce
that, so the gate no longer refuses an artifact because of what a value
said.

## Failure modes

- **A value names a known placeholder.** Printed as text. The slot the
  name refers to is filled once, from its own value.
- **A value names an unknown placeholder.** Printed as text. The render
  succeeds. Today it fails.
- **A value holds a single brace** (`C++ {templates}`). Encoded the same
  way. The page shows the same characters.
- **A value already holds `&#123;` as typed text.** `html.escape` turns
  the `&` into `&amp;`, so the page shows `&#123;`, the text the operator
  typed. HTML blocks built by the renderer escape their text the same way
  before the fill.
- **The template names a placeholder no value exists for.** The render
  fails by name, as today. Covered by
  `services/api/tests/test_resume.py::test_stale_template_with_old_education_placeholders_fails_the_render`.
- **A value exists for a name the template does not use.** Ignored, as
  today. The cover letter template has no `phone`; the industry and
  academic templates use the same names.
- **The stylesheet contains braces.** It always does. It is inlined into
  the template before the fill and passes through untouched.
- **A placeholder inside an attribute** (`href="mailto:{{email}}"`). The
  encoded braces are character references, which the browser decodes in
  attributes as in text.

## Acceptance criteria

- [ ] With `profile_summary`, `candidate.profile_lead`, `candidate.name`
      or `certifications[0]` set to a value containing
      `{{experience_html}}`, `{{education_html}}` or `{{profile}}`, the
      rendered resume has as many role articles and education items as the
      unmodified bundle, and shows the placeholder text once, inside that
      field's own slot
      (planned test_a_placeholder_in_a_bundle_string_is_printed_not_filled).
- [ ] Every string in the example bundle, set in turn to each placeholder
      name the industry and academic templates use, is either refused
      before rendering or renders with every other template slot holding
      exactly what it holds for the unmodified bundle. Slots are compared
      through a probe template with one tagged slot per placeholder, the
      method spec 062 used
      (planned test_no_bundle_string_fills_a_template_slot).
- [ ] A `profile_summary` containing `{{nope}}` renders, and the HTML shows
      `{{nope}}` as text
      (planned test_an_unknown_placeholder_in_a_value_renders_as_text).
- [ ] The cover letter for job title `Engineer {{full_version_html}}` has
      as many body paragraphs as the letter for `Engineer`, and its
      headline shows the title's braces as text
      (planned test_a_job_title_with_a_placeholder_prints_once_in_the_letter).
- [ ] A cover letter body containing `{{name}}` renders and shows the text
      `{{name}}`
      (planned test_a_letter_body_with_a_placeholder_renders).
- [ ] `validate_rendered_pdf` over the rendered HTML of a bundle whose
      `profile_summary` contains `{{experience_html}}` reports no
      placeholder error
      (planned test_the_gate_passes_a_value_that_prints_braces).
- [ ] The `<style>` element of the rendered resume and cover letter holds
      the stylesheet file's text unchanged
      (planned test_the_inlined_stylesheet_keeps_its_braces).
- [ ] A template placeholder with no value still fails the render by name
      (`services/api/tests/test_resume.py::test_stale_template_with_old_education_placeholders_fails_the_render`,
      unchanged, and planned
      test_a_cover_letter_template_placeholder_with_no_value_fails_by_name).
- [ ] Each new test fails on `main` at `4fa400b` and passes with the
      change. The pull request states that it ran them against both.

## Honest limitations

- The sweep criterion walks the example bundle. A string field the example
  bundle does not carry is covered by the fill being one function, not by
  the sweep.
- The job title and letter body cases use one synthetic title and one
  synthetic letter each. They prove the mechanism, not every posting.
- The page shows what the value said. An operator who types `{{nope}}`
  into their summary will see `{{nope}}` on their CV. This spec stops the
  text from being executed; it does not judge whether the text belongs on
  a CV.

## Options weighed

1. **Refuse `{{` in every emitted bundle string at parse time**
   (`_check_emitted` in `services/api/src/harrier/resume/content.py`).
   Rejected as the fix. It reaches only bundle strings. The job title, the
   model-written letter and brief-confirmed skills never pass through the
   parser, and the cover letter title case above is third-party text. It
   also refuses operator prose that did nothing wrong once the renderer is
   fixed.
2. **Single-pass fill only.** Closes the duplication. But a value
   containing `{{name}}` still reaches the output HTML as `{{name}}`, and
   the PDF gate then refuses the artifact with "HTML contains unresolved
   template placeholders" after a full Playwright render, blaming the
   template for a value.
3. **Single-pass fill with encoded braces** (this spec). One function at
   the sink, for every value from every source, and the gate's check keeps
   meaning "the template was not filled".

## Decisions recorded (Akin, 2026-10-11)

- **Cover letter in scope.** The finding named the resume. The cover
  letter has the same shape and the only third-party input reproduced
  here, so this spec covers both renderers with one function. Agreed: one
  spec, not split.
- **Literal braces on a CV.** This spec prints them. Agreed: no parse-time
  refusal of `{{` in operator prose is added.

## Data and privacy

All fixtures are the synthetic example bundle and the synthetic letter in
`services/api/tests/test_apply.py`. Error messages keep naming
placeholders only. No new file outside `specs/` and the tests; both are
public under `config/data-classification.json`.

## Migration

None. A render that failed because a value named an unknown placeholder
now succeeds. No stored data changes.

## Proof / origin

The local review of PR #282 (spec 130) reported the `profile_lead` case.
The table in Problem was produced by a scratch script and a scratch test,
neither committed, calling `parse_bundle`, `build_content_plan(bundle, "",
"Senior Frontend Engineer", date(2026, 8, 1))`, `build_markdown` and
`render_html` on the example bundle, and `render_cover_letter_html` on the
`db` fixture with `FULL_LETTER`, against the repo `templates/`.

## Out of scope

- Any parse-time rule on bundle strings (option 1).
- The PDF gate's own checks.
- Placeholder syntax: `{{ name }}` with spaces stays not a placeholder.
- The markdown-structure rules of spec 062 and the dash rules of spec 071.
- Escaping of any character other than `{` and `}` beyond what
  `html.escape` does today.
