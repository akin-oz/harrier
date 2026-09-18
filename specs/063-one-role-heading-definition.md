---
spec: 063
title: The resume role heading has one definition that the writer, the parser, and the validator share
status: proposed
approved: no
milestone: M8
depends: [013, 062]
---

# Spec 063: The resume role heading has one definition that the writer, the parser, and the validator share

## Problem

Each role in the resume markdown is one line:
`### <organization> <sep> <title> (<employment type>)`, where `<sep>`
is space, U+2014, space and the parenthesised suffix is present only
when the role has an employment type. Three places know that format,
and each knows it separately:

- the writer, `build_markdown` in
  `services/api/src/harrier/resume/markdown.py` (line 161), builds the
  line with an f-string that carries the separator as a literal;
- the parser, `_parse_experience` in
  `services/api/src/harrier/resume/htmlrender.py` (line 57), splits it
  back with `company_title.split(<sep>, 1)`, a second literal;
- the validator, `_splits_at_its_end` in
  `services/api/src/harrier/resume/content.py`, holds a third copy,
  `TITLE_SEPARATOR`, and re-enacts both of the others by hand: it
  trims the organization, appends the separator, and looks for the
  first occurrence, because that is what the writer and the parser
  happen to do today.

The validator is spec 062's Rule 3, and it is correct only while the
other two stay exactly as they are. Nothing enforces that. Spec 062
could not fix it, because its scope forbade touching `markdown.py` and
`htmlrender.py`; both local reviews (of PR #73 and PR #74) recorded it
as a finding and left it for its own spec. This is that spec.

Nothing is broken for the operator today. What is broken is that the
next person to change the heading format reopens spec 062's hole with
every test green. The old system had this in one place for the writer
(`role_heading` in `~/job-hunt-local/scripts/tailor_resume.py`, lines
1174 to 1178) and a literal in the parser (line 1295); the port kept
the two literals and spec 062 added the third.

### What was run

On `origin/main` at `7e2da1b`, in a scratch worktree, with
`HARRIER_DATA_DIR` on a scratch directory. Nothing from these
experiments is in any branch.

- **Writer and parser changed together, validator untouched.** Both
  literals replaced with ` | `. `pytest tests/test_resume.py`: 102
  passed. Then an organization of `Acme | Talent` passes
  `parse_bundle`, and `render_html` shows the company as `Acme` and the
  title as `Talent | Senior Frontend Engineer (Freelance)`. That is
  the defect spec 062 closed, back, and no test noticed. The separator
  tests could not notice: they assert against the validator or against
  the test file's own copy of the separator.
- **Writer changed alone.** 11 of the 102 tests fail, 10 of them with
  `ValueError: not enough values to unpack (expected 2, got 1)` from
  the parser. Drift between writer and parser is loud. It is drift
  between those two and the validator that is silent.
- **A role heading with no separator** (a hand-edited markdown passed
  to `render_html`, `### Acme Talent` under `## EXPERIENCE`): the same
  unnamed `ValueError: not enough values to unpack (expected 2, got
  1)`. Spec 059 gave the education parser a defined answer for a
  hand-edited section; the experience parser has none.

## Scope

- A new module, `services/api/src/harrier/resume/heading.py`: the one
  definition. Public source like its neighbours; it holds a format and
  no data, so the classification table needs no entry.
- `services/api/src/harrier/resume/markdown.py`: `build_markdown`
  writes the role heading through it.
- `services/api/src/harrier/resume/htmlrender.py`: `_parse_experience`
  splits the role heading through it.
- `services/api/src/harrier/resume/content.py`: spec 062's Rule 3 asks
  it, and `TITLE_SEPARATOR` and `_splits_at_its_end` go.
- `services/api/tests/test_resume.py`: the tests described below.

No contract, tracker, template, or web change. No change to
`config/resume-content.example.json`.

## Behavior

**One definition.** The separator is written once, in `heading.py`,
next to one function that writes a role heading from an organization,
a title, and an employment type, and one that splits a heading line
back into the organization and the rest. The writer calls the first.
The parser calls the second. The validator calls both: a role's
organization is acceptable when the heading written for that role
splits back into that organization, trimmed as the loader trims it.

**Nothing the operator sees changes.** For every bundle that
`parse_bundle` accepts today, the markdown, the HTML, and the PDF are
byte-identical to today's. The heading line keeps its exact text:
`### `, the organization, space, U+2014, space, the title, and ` (<type>)`
when the role has an employment type. The parser still splits on the
first separator, so a title may still contain one, and the rendered
title still carries the employment type suffix.

**The validator gives the same verdicts, for a reason that can no
longer drift.** Every organization spec 062 refuses is still refused,
with the same message, `roles[<i>].organization must not contain the
title separator or end with its dash`, and every one it accepts is
still accepted. The difference is that the verdict now follows the
writer and the parser. If the one definition changes (another
separator, a different split), the validator guards the new format
without being edited, and a test proves it by changing the definition
and watching all three move.

**A role heading that cannot be split has a named error.** When a
`### ` line under `## EXPERIENCE` has no separator, the split function
raises `ValueError` with the message `role heading <n> has no title
separator`, where `<n>` is the 1-based position of that heading in the
section. It names a position, not the line's text, because the text is
an employer's name and error messages reach logs. This replaces
today's unnamed unpacking error. It is reachable only with a markdown
that `build_markdown` did not write.

## Failure modes

- **The separator is changed in its one definition**: the writer emits
  the new one, the parser splits on it, and the validator refuses an
  organization that contains it or ends with its leading part. An
  organization containing the old separator becomes acceptable, which
  is correct: it no longer splits early.
- **Someone reintroduces a literal** in the writer or the parser
  instead of calling the shared functions: the drift test fails,
  because changing the definition no longer moves that site.
- **A title containing the separator**: unchanged. The split is on the
  first occurrence, and the validator guarantees the first occurrence
  is the writer's own.
- **A role with no employment type**: unchanged, no suffix and no
  trailing space.
- **Hand-edited markdown with a role heading that has no separator**:
  the named `ValueError` above. `run_tailor` never produces this, so
  the tracker and the artifact directory are not involved.
- **A `ResumeBundle` that did not come from `parse_bundle`** (built
  with `dataclasses.replace`, as some tests do): the writer still
  trusts it, as today. See Out of scope.

## Acceptance criteria

The new tests live in `services/api/tests/test_resume.py`. They are
described here and named when they exist, because
`services/api/tests/test_spec_structure.py::test_every_test_a_spec_names_actually_exists`
fails on a test symbol that is not yet defined. The implementing change
amends this section with each name. Tests that already exist are named.

- [ ] With the separator changed to ` | ` in its one definition, for
  the duration of one test: the markdown role heading is written with
  ` | `; `render_html` shows the first role's company and title
  unchanged from the unpatched render; `parse_bundle` refuses an
  organization of `Acme | Talent` with spec 062's Rule 3 message; and
  an organization containing the old separator parses and renders as
  the company intact; a test pins it. This is the test that cannot be
  written today: the experiment above is its failing form.
- [ ] The heading line for the example bundle's first role is asserted
  literally, character for character including U+2014 and the
  `(Freelance)` suffix, so changing the separator is a visible,
  deliberate act; a test pins it.
- [ ] The markdown and the HTML for the unmodified example bundle are
  byte-identical before and after the change. The reviewer checks this
  by rendering on `main` and on the branch; the existing render tests
  (`test_html_header_uses_grounded_markdown_title`,
  `test_html_renders_every_degree_newest_first`) keep passing
  unedited.
- [ ] Spec 062's separator tests pass unedited:
  `test_organization_containing_the_title_separator_is_refused`,
  `test_organization_ending_in_the_separators_dash_is_refused`,
  `test_organization_made_of_dashes_elsewhere_still_splits_exactly`,
  `test_title_containing_the_separator_stays_one_role`.
- [ ] `render_html` on a markdown whose `## EXPERIENCE` section holds a
  `### ` line with no separator raises `ValueError` matching `role
  heading 1 has no title separator`; a test pins it.
- [ ] A role with an empty employment type writes a heading with no
  suffix and no trailing space, and it splits back to the same
  organization and title; a test pins it.
- [ ] `git grep -n 'u2014' services/api/src/harrier/resume` and the
  same search for the literal character show the role heading
  separator defined once, in `heading.py`. The remaining hits are the
  resume headline in `plan.py` (`build_presentation_title`) and
  `normalize_visible_role_title` in `markdown.py`, which are other
  formats. `TITLE_SEPARATOR` and `_splits_at_its_end` no longer exist
  in `content.py`. Checked by the reviewer against the diff, not by a
  test: a test that reads source text is a last resort.
- [ ] The import-linter contracts stay kept, and `heading.py` imports
  nothing from `harrier.resume`, so `content.py`, `markdown.py`, and
  `htmlrender.py` can all import it without a cycle.
- [ ] The diff touches only the five files in Scope and this spec.
- [ ] `just gate` passes.

## Proof / origin

- The three sites: `markdown.py` line 161, `htmlrender.py` line 57,
  `content.py` `TITLE_SEPARATOR` and `_splits_at_its_end`.
- The rule being protected: spec 062, Rule 3, and its amendments
  section, which lists this as not changed and out of scope.
- The findings: finding 3 of the local review of PR #73 ("Separator
  constant duplicated across three modules") and finding 3 of the
  local review of PR #74 ("Validator emulates writer and parser by
  hand"), both recorded in the PR #74 description.
- Old system: `~/job-hunt-local/scripts/tailor_resume.py`, `role_heading`
  at lines 1174 to 1178 and the parser's split at line 1295.

## Out of scope

- **The other structural markers.** `## ` section headings, `### `
  entry markers, `- ` bullets, and the education entry format
  (`_education_lines` in `markdown.py`, `_parse_education` in
  `htmlrender.py`, the `school` rule in `content.py`) are known in
  more than one place in the same way. They share this problem's
  shape. They are not in this spec because none of them has a
  validator re-enacting a split by hand, which is what made the role
  heading the one that could reopen silently.
- **A writer that checks its own input.** `build_markdown` still
  trusts a `ResumeBundle` or a `ContentPlan` that did not come through
  `parse_bundle`. Having the writer refuse a heading that does not
  split back would be cheap once the functions are shared, and it is
  left out on purpose: it is one part of the structural backstop spec
  062 names as its own spec, and adding a slice of it here would make
  that spec harder to write.
- **The rendered title carrying the employment type suffix.** The
  parser returns everything after the first separator as the title,
  suffix included, and the template shows it that way. Unchanged.
- **The resume headline** (`build_presentation_title` in `plan.py`) and
  `normalize_visible_role_title`, which use the same character for
  other purposes.
- **The test file's own copy of the separator**
  (`TITLE_SEPARATOR` in `test_resume.py`). It stays: tests that state
  the expected character independently of the code are what make a
  change to it visible.
- Any change to spec 062's error messages, rules, or tests.
- The cover letter, answers, and outreach paths.
- Any contract, tracker, template, or web change.

## Migration

None. No bundle that parses today stops parsing, no output changes,
and no stored document needs editing. The only new behaviour an
operator could meet is the named error for a hand-edited markdown,
which replaces an unnamed one.
