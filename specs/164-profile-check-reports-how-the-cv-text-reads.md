---
spec: 164
title: profile check reports how the CV's own text reads, without refusing it
status: accepted
approved: yes
milestone: M9
depends: [071, 098, 099, 130, 144]
---

# Spec 164: profile check reports how the CV's own text reads, without refusing it

## Problem

Every line of a tailored CV is the operator's verified text: a pool bullet,
the framing's `profile_summary`, `profile_lead` and `experience_statement`.
The model only selects and orders it (product invariant "Verified content
only"). So the CV can never read better than those lines do, and no code
tells the operator how they read.

University CV guidance asks four things of that text. The University of
Edinburgh Careers Service states them on its "Improving your CV" and
"Building your CV" pages (see Proof / origin):

- **Lead with an active verb.** "In charge of staff rota" is called purely
  descriptive. A line should open with what the candidate did.
- **Show impact.** Evidence plus an active verb plus a measured outcome,
  "where you can use facts and figures". Not every line can carry a number.
- **Implied first person.** "Experienced in programming using Python", not
  "I am experienced".
- **No unevidenced self-description.** A profile that says "hard-working"
  adds nothing.

Its cover letter guidance adds two more that apply to any line the
candidate writes about themself: no negative framing ("I had to", "I was
required to") and no passive voice about one's own growth ("my skills were
strengthened by").

Today `profile check` (`check_resume`,
`services/api/src/harrier/resume/framing.py`) runs the truth gate only. A
weak line passes it, renders, and is sent.

## Scope

- `services/api/src/harrier/resume/writing.py` (new): the writing rules and
  `writing_findings(bundle) -> list[str]`. Deterministic: no model call, no
  network.
- `services/api/src/harrier_cli/main.py`: `_cmd_profile_check` prints the
  findings after `check_resume`'s lines.
- Tests: `services/api/tests/test_cv_writing.py` (new).

`check_resume` itself does not change. `profile put` compares its lines
before and after an edit to decide whether the edit broke a track
(`_check_state`, same file). A writing finding is not a break, so it must
never enter those lines.

## Behavior

### What is read

The scope's bundle (`load_bundle`): every `bullet_pool` line, and the
framing strings `profile_summary`, `profile_lead` and
`experience_statement` when set. A role's lines are the pool ids that start
with `<role.id>_`. Achievement lines are the ids that start with `ach_`, as
`build_content_plan` reads them (`services/api/src/harrier/resume/plan.py`).

### The rules

Each rule matches on word boundaries, case-insensitively unless the row
says otherwise.

| # | Rule | Applies to | Finding |
|---|---|---|---|
| W1 | **Weak opener.** The line begins with: responsible for, worked on, working on, helped, assisted, involved in, participated in, tasked with, duties included, in charge of, was, were. | pool lines | `bullet <id>: opens with "<phrase>"; lead with what was done` |
| W2 | **First person.** The line contains the word "I" written as a capital, or me, my, mine, we, us, our in any case except "US" in capitals (the country). | pool lines, framing strings | `<id or field>: first person ("<word>"); use the implied first person` |
| W3 | **Achievement without a figure.** An `ach_` line with no number token (`number_tokens`, `services/api/src/harrier/apply/claims.py`). | `ach_` lines | `bullet <id>: an achievement with no figure` |
| W4 | **Role figure count.** One line per role: how many of its lines carry a number token. Printed for every role, as information. | role lines | `role <role.id>: <k> of <n> bullets state a figure` |
| W5 | **Repeated opener.** Two or more lines of one role, or two or more `ach_` lines, begin with the same first word. | role lines, `ach_` lines | `role <role.id>: <n> bullets open with "<word>"` (or `achievements:`) |
| W6 | **Negative or passive framing.** had to, was required to, were required to, was given the opportunity, or a form of "was/were" + developed, strengthened, improved, gained or built + by or through. | pool lines, framing strings | `<id or field>: negative or passive framing ("<match>")` |
| W7 | **Self-description without evidence.** hard-working, hardworking, team player, passionate, highly motivated, self-starter, detail-oriented, results-driven, strong work ethic, excellent communication skills. | framing strings | `<field>: self-description ("<phrase>"); show it with a fact instead` |
| W8 | **Long line.** A pool line over 35 words or of more than two sentences (`sentence_count`, `plan.py`). | pool lines | `bullet <id>: <n> words; keep a bullet under 35` |

A finding names the bullet id or the framing field and the matched phrase.
It never prints the line's text, as `check_resume` never does.

### Output and exit code

`harrier profile check` prints, in order:

1. `check_resume`'s lines, unchanged.
2. When any W1 to W8 finding exists other than W4: the line
   `writing (advisory):` and then each finding, W4's role lines last.
   When only W4 lines exist, they print under `writing (advisory):` too.

The exit code is `check_resume`'s. A writing finding never changes it, and
never blocks `profile put`, `tailor` or rendering.

### Failure modes

- **The bundle does not load.** `check_resume` exits 1 with its message,
  and no writing findings print: there is nothing to read.
- **A line legitimately opens with a W1 phrase** (for example a role whose
  real duty was "Responsible for ..."). The finding prints every run. The
  operator rewrites the line through `profile put`, or leaves it.
- **A word list misses a phrasing.** It passes. The lists are fixed in
  `writing.py`; extending one is a change to this spec.

## Acceptance criteria

- [ ] A synthetic bundle with one line breaking each of W1, W2, W3, W5, W6,
      W7 and W8 prints that rule's finding with the bullet id or field, and
      the same bundle with the line fixed prints none for it (planned test
      in test_cv_writing.py: test_each_writing_rule_finds_and_its_fix_clears).
- [ ] W4 prints "2 of 3" for a role with three lines, two of which hold a
      number (planned test: test_role_figure_count).
- [ ] "myself" does not trigger W2 on "my", "API" and "i.e." do not
      trigger it on "I", and "US payroll" does not trigger it on "us"
      (planned test: test_first_person_matches_whole_words).
- [ ] No finding line contains any pool line's text (planned test:
      test_findings_name_ids_never_text).
- [ ] `profile check` exits 0 for a bundle whose bullets pass the truth gate
      and that has writing findings, and prints them under
      "writing (advisory):" (planned test: test_profile_check_exit_ignores_writing).
- [ ] A `profile put` that adds a W1 line to a valid track is stored: a
      writing finding is not a break (planned test:
      test_a_writing_finding_does_not_refuse_a_put).
- [ ] The existing `profile check` tests in
      `services/api/tests/test_track_framing.py` pass unchanged.

## Proof / origin

- University of Edinburgh Careers Service, "Improving your CV"
  (https://careers.ed.ac.uk/cvs-and-applications/improving-your-cv):
  evidence, active verbs, impact statements, and the rota example.
- Same, "Building your CV"
  (https://careers.ed.ac.uk/cvs-and-applications/building-your-cv): implied
  first person, a profile that avoids "I am hard-working".
- Same, "Writing a cover letter"
  (https://careers.ed.ac.uk/cvs-and-applications/writing-a-cover-letter),
  tip 7: negative tone and passive voice.
- The example CVs the Careers Service publishes through CareerSet repeat
  these points in their margin notes: start bullets with an action verb,
  vary the verbs, quantify where possible. They are not committed.
- `check_resume` and `_check_state` in
  `services/api/src/harrier/resume/framing.py`; the pool id prefixes in
  `build_content_plan`, `services/api/src/harrier/resume/plan.py`.

## Out of scope

- Rewriting any line. The operator edits the truth sources and the pool
  through `profile put` (spec 144). Nothing here writes a profile document.
- Refusing a render on a writing finding. A finding is advice.
- Running the rules inside `tailor` on the selected lines, and showing them
  in the web app. Each is its own change once the rules have been used.
- Spelling variant consistency (British and American forms in one CV).
- The CV's page count and font. The industry kind prints one page
  (`KindRules.pages`, `services/api/src/harrier/tracks.py`); the Edinburgh
  guidance's two-page UK default is an open decision below, not part of
  this spec.

## Honest limitations

- Every rule is a word list or a count. It cannot tell a strong line from a
  weak one that avoids the listed words.
- W3 and W4 count digits. "Cut build time in half" carries a figure in words
  and is reported as having none.
- W8's 35 words is a judgment, not a number from the guidance. The guidance
  says concise and easy to read, nothing more exact.

## Open decisions for Akin

1. **Advisory, or a `--strict` flag.** This spec never changes the exit
   code. A `--strict` flag that exits 3 on any finding would let CI or a
   pre-send check gate on it. Add it here, or later?
2. **W8's limit.** 35 words, or another number?
3. **Page count.** The Edinburgh guidance calls two pages the UK standard.
   The industry kind prints one. Keep one page (common for remote software
   roles), or make the page count a framing option in its own spec?

## Decisions taken (2026-10-26)

Akin approved the spec as written, which settles the open decisions above
as the spec states them.
