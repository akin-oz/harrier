---
spec: 063
title: The resume role heading has one definition that the writer, the parser, and the validator share
status: accepted
approved: yes
milestone: M8
depends: [013, 062]
---

# Spec 063: The resume role heading has one definition that the writer, the parser, and the validator share

## Problem

Each role in the resume markdown is one line:
`### <organization> <sep> <title> (<employment type>)`, where `<sep>`
is space, U+2014, space and the parenthesised suffix is present only
when the role has an employment type. Three places know that format,
and each knows it separately (line numbers are `main` at `7e2da1b`):

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

### Why this is a spec and not a refactor

Sharing one definition changes no output, and the rules say a refactor
needs no spec. Two things make this more than one. The named error
below is new behaviour, small as it is. And the guarantee being bought,
that the validator follows the writer and the parser when either
changes, is not visible in any output today; it is visible only in the
test that changes the definition, which is why that test is the first
acceptance criterion. If the named error is struck at approval, what
remains is a refactor plus that test, and it could land under spec 062
instead, with 062's scope line amended to admit the two files. That is
the approver's call; this spec is written so either works.

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
  definition. Classified public: `config/data-classification.json`
  defines public as the complement of its `never_in_git` list, no
  entry there matches `services/api/src`, and the file holds a format
  and no data.
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
byte-identical to today's. The role heading is the only line whose
construction moves, so the proof is the test that asserts that line
literally, with the render tests that already exist. The heading line keeps its exact text:
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
an employer's name and an error message is the kind of text that gets
pasted into a terminal, a CI log, or a bug report. Nothing in harrier
logs it today: the only caller that prints a resume error is
`_cmd_tailor` in `services/api/src/harrier_cli/main.py`, to stderr. This replaces
today's unnamed unpacking error. It is reachable only with a markdown
that `build_markdown` did not write.

## Failure modes

- **The separator is changed in its one definition**: the writer emits
  the new one, the parser splits on it, and the validator refuses an
  organization that contains it or ends with its leading part. An
  organization containing the old separator becomes acceptable, which
  is correct: it no longer splits early.
- **The message under a changed separator**: spec 062's Rule 3
  message says "or end with its dash". With another separator that
  word would be wrong. The message is not made generic here, because
  that would edit approved spec 062 text for a separator nobody has
  proposed; whoever changes the separator changes the message with it.
  The drift test asserts the message as it stands.
- **A role whose title is missing or not a string**: already refused by
  name (`roles[<i>]: missing or empty title`). The organization check
  needs a title to write the heading with, so it runs only when both
  are strings, and its verdict does not depend on what the title says.
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

Every test named here is in `services/api/tests/test_resume.py`. The
spec was approved describing the new ones, because
`services/api/tests/test_spec_structure.py::test_every_test_a_spec_names_actually_exists`
fails on a test symbol that is not yet defined; the implementing change
named them.

- [ ] With the separator changed to ` | ` in its one definition, for
  the duration of one test: the markdown role heading is written with
  ` | `; `render_html` shows the first role's company and title
  unchanged from the unpatched render; `parse_bundle` refuses an
  organization of `Acme | Talent` with spec 062's Rule 3 message; and
  an organization containing the old separator parses and renders as
  the company intact:
  `test_changing_the_one_separator_moves_writer_parser_and_validator`.
  Before this change the test could not be written; the experiment
  above was its failing form. It fails again if any one of the three
  sites goes back to its own copy of the separator (each tried during
  implementation).
- [ ] The heading line for the example bundle's first role is asserted
  literally, character for character including U+2014 and the
  `(Freelance)` suffix, so changing the separator is a visible,
  deliberate act:
  `test_role_heading_line_is_written_exactly_as_it_always_was`.
- [ ] Output is unchanged. The proof is the literal heading line
  above, which is the only line whose construction moves, together
  with the existing render tests passing unedited
  (`test_html_header_uses_grounded_markdown_title`,
  `test_html_renders_every_degree_newest_first`,
  `test_title_containing_the_separator_stays_one_role`). A reviewer who
  wants more can diff the markdown for the example bundle between
  `main` and the branch; that is a check, not a criterion.
- [ ] Spec 062's separator tests pass unedited:
  `test_organization_containing_the_title_separator_is_refused`,
  `test_organization_ending_in_the_separators_dash_is_refused`,
  `test_organization_made_of_dashes_elsewhere_still_splits_exactly`,
  `test_title_containing_the_separator_stays_one_role`.
- [ ] `render_html` on a markdown whose `## EXPERIENCE` section holds a
  `### ` line with no separator raises `ValueError` matching `role
  heading 1 has no title separator`, and `role heading 2` when the
  second heading is the broken one, and the message carries none of the
  line's text:
  `test_role_heading_with_no_separator_has_a_named_error`.
- [ ] A role with an empty employment type writes a heading with no
  suffix and no trailing space, and it splits back to the same
  organization and title:
  `test_role_without_an_employment_type_has_no_suffix_and_splits_back`.
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
  more than one place in the same way, and they share this problem's
  shape, silent drift included: spec 062's Rule 2
  (`_starts_with_heading_marker` in `content.py`) is the validator
  holding the parser's `## ` and `### ` markers (`htmlrender.py`,
  `_extract_section`, `_parse_experience`, `_parse_education`) by hand,
  and if those markers changed it would guard the wrong character with
  every test green. An earlier draft of this section said otherwise
  and was wrong. They are left out for two honest reasons. The markers
  are markdown's own syntax, which this codebase has far less reason
  to change than a separator it chose itself. And sharing them means
  the renderer stops re-parsing markdown and reads the plan, which is
  a redesign with its own spec, not a slice of this one.
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
