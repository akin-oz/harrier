---
spec: 142
title: A LinkedIn link never carries an unsafe scheme
status: proposed
approved: no
milestone: M9
depends: [013, 014, 034, 140]
---

# Spec 142: A LinkedIn link never carries an unsafe scheme

## Problem

Every CV and every cover letter prints the candidate's LinkedIn link as a
live href. The value goes into the page as written, escaped for HTML and
not checked for its scheme:

- The CVs read `candidate.linkedin` from the resume facts
  (`render_html` in `services/api/src/harrier/resume/htmlrender.py`, the
  `{{linkedin_url}}` placeholder of `templates/resume-template.html` and
  `templates/resume-template-academic.html`).
- The cover letter reads `candidate.linkedin` from the candidate document,
  or, when that is empty, the `- LinkedIn:` line of the truth document
  (`candidate_contact` in `services/api/src/harrier/apply/letters.py`,
  `templates/cover-letter-template.html`).

`html.escape` does not disable a scheme. A value such as
`javascript:alert(1)` becomes a link that runs script when the HTML page is
opened and the link clicked. The values are the operator's own, so this is
not an outside attack today, but a document copied from elsewhere or a typo
in a hand-written truth line would ship a broken or unsafe link in a
recruiter-facing document.

CodeRabbit's review of PR #343 found this shape in spec 140's new links,
and spec 140 refuses them (its amendment 9). The LinkedIn field is older
than spec 140 and outside it; the review asked for it to be handled here.

## Scope

- `services/api/src/harrier/resume/content.py`: the `candidate.linkedin`
  check, where the facts are parsed.
- `services/api/src/harrier/apply/letters.py`: the same check on the value
  `candidate_contact` returns.
- Tests in `services/api/tests/test_resume.py` and
  `services/api/tests/test_apply.py`.

## Behavior

One rule, for every LinkedIn value harrier renders: **a value that names a
scheme must name `http` or `https`.** A value with no scheme, such as
`linkedin.com/in/example`, renders as it does today. Matching is
case-insensitive, and leading spaces do not hide a scheme.

- **The CV.** A resume facts document whose `candidate.linkedin` names any
  other scheme is refused when it is read, with "candidate.linkedin must be
  an http or https URL" among the bundle's errors. `profile check` and
  `profile put resume_framing` report it; `tailor` writes no CV. This holds
  on every kind, the focused header of spec 140 included, which reads the
  same field.
- **The letter.** When the value `candidate_contact` returns, from the
  candidate document or from the truth document's `- LinkedIn:` line,
  names any other scheme, the letter's HTML is not rendered: the run fails
  with "the candidate's LinkedIn link must be an http or https URL", no
  HTML or PDF is written, and the markdown draft is kept, as for any other
  render failure (spec 067).

Nothing is rewritten. A refused value is the operator's to correct.

## Failure modes

- **A value with no scheme.** Accepted and rendered as today. It is a
  relative link in the HTML, which is the behavior every existing document
  already has.
- **An empty value.** Unchanged: the facts already require
  `candidate.linkedin`, and the letter renders an empty link as today.
- **A value in the truth document only.** Checked the same way, because
  the letter reads it when the candidate document has none.

## Acceptance criteria

- [ ] A resume facts document with `candidate.linkedin` of
  `javascript:alert(1)`, in any case and with leading spaces, is refused
  naming the field (planned test_an_unsafe_linkedin_scheme_is_refused).
- [ ] With `https://` and with no scheme, the industry CV's markdown and
  HTML are byte-identical to today's (planned
  test_a_safe_linkedin_renders_as_before).
- [ ] A candidate document whose `linkedin` names `javascript:` fails the
  letter run with no HTML or PDF, and the draft kept (planned
  test_a_letter_with_an_unsafe_linkedin_is_not_rendered).
- [ ] A truth line `- LinkedIn: javascript:...`, with no candidate value,
  fails the same way (planned test_a_truth_line_linkedin_is_checked_too).
- [ ] `just check` passes.

## Proof / origin

- The finding: CodeRabbit's review of PR #343, on
  `services/api/src/harrier/resume/htmlrender.py`, and its question about
  `candidate.linkedin` on the same thread.
- The same rule for spec 140's links: amendment 9 of
  `specs/140-a-focused-tracks-documents-present-a-smaller-scope-honestly.md`.
- Where the value is rendered: the templates and functions named under
  Problem.

## Out of scope

- Requiring a scheme, or adding `https://` to a value without one.
- The other bundle list fields that drop a non-string entry rather than
  refusing it (`all_skills`, `positioning_technologies` and the rest).
  They are a separate finding of the same review, for their own spec.
- The email link: it is rendered behind a fixed `mailto:` and cannot name
  another scheme.
- Checking that a link resolves. Links are facts the operator writes.

## Migration

None, unless a stored LinkedIn value names a scheme other than http or
https; `profile check` then names it, and the operator corrects it.
