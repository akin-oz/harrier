---
spec: 140
title: A focused track's documents present a chosen smaller scope honestly
status: accepted
approved: yes
milestone: M9
depends: [034, 065, 071, 098, 099, 101, 130, 137]
---

# Spec 140: A focused track's documents present a chosen smaller scope honestly

## Problem

Spec 137 refuses `tailor`, `cover-letter` and `answers` on a focused track,
because every document shape in harrier tells one of two stories. The
industry documents present a senior engineer looking for broad ownership.
The academic documents present a research candidate. A bounded role needs
a third story, told from the same facts: a senior engineer choosing a
smaller scope on purpose, who takes designs to production and does not
ask for broad ownership.

The operator wrote that story by hand outside harrier, as a resume and a
letter for one bounded posting. Producing it through harrier hits these
limits:

- **The header cannot carry what the story needs.**
  `templates/resume-template.html` prints location, email, LinkedIn and
  phone. `ResumeBundle` has no GitHub link, no portfolio or demo link and
  no line saying how the candidate contracts. The letter template has the
  same header and no salutation.
- **The headline is derived.** `build_presentation_title`
  (`services/api/src/harrier/resume/plan.py`) appends up to two ranked
  technologies to `primary_identity` after a comma. A fixed headline such
  as "Frontend Engineer | React, Next.js & TypeScript" cannot be set.
- **No section holds AI-assisted engineering work.** `cv_sections` is a
  permutation of the six industry headings (spec 101).
- **The letter has three paragraphs and 240 words** (`KindRules`). The
  story needs six: the opening, the proof, the AI work, why the scope is
  smaller, how the candidate invoices, and the demo.
- **An unverified skill the posting asks for disappears silently.** The
  validator drops it, which is correct. The operator never learns the
  posting asked for it, so a skill that is true but unrecorded stays
  unrecorded.

## Scope

- `services/api/src/harrier/resume/content.py` and `documents.py`:
  optional `github` and `links` in the facts; optional `headline`,
  `entity_line` and `ai_bullets` in the framing.
- `services/api/src/harrier/resume/plan.py`: the framing's `headline`,
  when set, is the title.
- `services/api/src/harrier/resume/markdown.py` and `htmlrender.py`: the
  `## AI-ASSISTED ENGINEERING` heading and the focused header.
- `templates/resume-template-focused.html` and
  `templates/cover-letter-template-focused.html` (new).
- `services/api/src/harrier/tracks.py`: the focused `KindRules` document
  fields; `tailor`, `cover-letter` and `answers` opened on the kind.
- `services/api/src/harrier/voices.py`: `FOCUSED_TAILOR_VOICE`,
  `FOCUSED_LETTER_VOICE`, `FOCUSED_ANSWERS_VOICE`.
- `services/api/src/harrier/resume/tailor.py`: the missing-keywords
  sidecar.
- `config/resume-framing.focused.example.json` and
  `config/application-profile.focused.example.json` (new, public,
  synthetic).
- Tests: `services/api/tests/test_focused_documents.py` (new), plus
  additions to `test_resume.py` and `test_resume_documents.py`.

## Behavior

### New fields, all optional

In `resume_facts` (shared, true on every track):

- `candidate.github`: a URL.
- `candidate.links`: at most two `{"label", "url"}` objects, for example a
  public demo.

The operator writes facts and vouches for them, the same standing that
`email` and `linkedin` have today. Each label and URL is checked like
every emitted candidate string: one line, no dash as punctuation.

In `resume_framing` (owned per track, spec 099):

- `headline`: one line. When set, it is the CV title verbatim, and
  `build_presentation_title` is not called.
- `entity_line`: one line of at most 90 characters, saying how the
  candidate contracts and from which time zone.
- `ai_bullets`: one or two bullet pool ids.

A framing without these fields produces exactly today's output on every
kind. The industry and academic templates do not read them.

### The focused CV

`KindRules["focused"]` sets:

- `cv_template`: `resume-template-focused.html`.
- `cv_sections`: PROFILE, SELECTED ACHIEVEMENTS, EXPERIENCE, AI-ASSISTED
  ENGINEERING, TECHNICAL SKILLS, EDUCATION, CERTIFICATIONS.
  `## AI-ASSISTED ENGINEERING` joins the known headings, and only a kind
  that lists it renders it.
- `pages`: `(1,)`.

The header prints the name, the headline, then email, LinkedIn, GitHub and
each link on one line, then the entity line on its own line. It prints no
location and no phone. A missing optional field leaves no empty separator.

The profile is the framing's `profile_lead` (spec 130), the experience
statement, and `profile_summary`. On the focused kind a profile of more
than three sentences is refused. The experience wording and year count are
checked as today (spec 071): the count is derived from the facts' career
start, and `year_counts` refuses any larger number.

The AI section prints the `ai_bullets` pool lines. They are truth-checked
like every other pool line.

Past job titles, employers, dates, education and certifications come from
the facts unchanged, as on every kind.

### The missing-keywords sidecar

On the focused kind only, `tailor` writes `<stem>.missing-keywords.md`
beside the draft. The industry and academic kinds write no sidecar of this
kind. It lists each technology term that the posting names and that is:

- in the skill vocabulary but not verified (spec 065), or
- in the framing's new optional `watch_terms` list and not contained in
  the truth sources.

Each entry quotes the posting line that names it. The sidecar is internal
metadata. It sits with the other sidecars, which are never-in-git under
the current classification, and never in the HTML or PDF. For example,
a posting asks for "Git", and the truth sources name only "GitHub
Actions": Git is not on the CV and is listed in the sidecar.

### The focused letter

`KindRules["focused"]` sets `letter_paragraphs` to 6 and
`letter_max_words` to 320. `FOCUSED_LETTER_VOICE` asks for six paragraphs,
in order:

1. Designs taken to production, with the experience wording from the
   facts.
2. Two or three proof points, each a declared claim with truth evidence
   (spec 065).
3. One short paragraph of AI-assisted engineering, from the truth sources.
4. Why the candidate chose a smaller scope. Drawn only from the track's
   application profile `scope_reason`, written as a stated preference,
   never as a claim about the employer or a fact without evidence.
5. How the candidate contracts and invoices. The facts in it (the entity,
   the time zone, any tax or VAT identifier) must be truth-source lines,
   like every candidate claim. A value the material lacks is written as
   `[[TODO: tax or VAT ID]]`, never invented.
6. The closing: the demo and the repository, from `candidate.links` and
   `candidate.github`.

`cover-letter-template-focused.html` renders "Hello <Company> team," above
the paragraphs and the candidate's name below them. Neither counts as a
paragraph. The writing rules are the shared ones (`LETTER_SHARED`, the
banned phrases, the dash check), plus two in the focused voice: short
sentences, and no question in the closing.

The placeholder rule is today's (spec 065, C10). The markdown draft keeps
the placeholder visible. The command exits 3 listing it, and no HTML or
PDF is written until the operator fills it.

### Answers

`answers` on a focused track uses `FOCUSED_ANSWERS_VOICE`, built from the
same scope and entity sources as the letter. The answer checks are
unchanged.

## Failure modes

- **A posting skill that is not verified.** Left off the CV and the
  letter, and listed in the sidecar. Never added.
- **A letter with a placeholder.** Exit 3, draft kept, no PDF (C10).
- **A letter citing the application profile as evidence.** Refused as
  today ("application profile cited as candidate evidence", spec 069).
- **A CV over one page.** Refused by the PDF gate. The draft is kept.
- **A framing headline with a dash as punctuation.** Refused when the
  framing is put (spec 071).
- **An industry framing that sets the new fields.** The headline is used,
  because it is the framing's choice. The industry template prints no
  GitHub, links or entity line, so the industry CV changes only by its
  headline.

## Acceptance criteria

- [ ] An industry tailor with today's framing and facts produces
  byte-identical markdown and HTML to the run before this change (planned
  test_the_industry_cv_is_unchanged).
- [ ] A focused tailor prints the framing's `headline` verbatim, and
  GitHub, the links and the entity line in the header, with no location or
  phone (planned test_the_focused_header_carries_links_and_the_entity_line).
- [ ] The focused CV's section headings appear in the `cv_sections` order,
  with the AI section holding the `ai_bullets` lines (planned
  test_the_focused_cv_has_an_ai_section).
- [ ] A focused framing whose profile has four sentences is refused
  (planned test_a_focused_profile_is_at_most_three_sentences).
- [ ] A posting that asks for Git, against truth sources that name only
  GitHub Actions, yields a CV without "Git" and a sidecar listing it with
  the posting line (planned test_an_unverified_posting_skill_goes_to_the_sidecar).
- [ ] An industry tailor of the same posting writes no missing-keywords
  sidecar (planned test_the_sidecar_is_focused_only).
- [ ] A focused letter with six paragraphs of at most 320 words passes. A
  letter with three is refused naming the count (planned
  test_the_focused_letter_has_six_paragraphs).
- [ ] A focused letter containing `[[TODO: tax or VAT ID]]` exits 3 with
  the draft kept and no PDF (planned test_a_focused_letter_placeholder_blocks_the_pdf).
- [ ] The focused letter's HTML starts with "Hello <Company> team," and
  ends with the candidate's name (planned test_the_focused_letter_has_a_salutation).
- [ ] `just check` passes.

## Proof / origin

- The header today: `templates/resume-template.html`,
  `templates/cover-letter-template.html`, and `render_html` in
  `services/api/src/harrier/resume/htmlrender.py`.
- The derived headline: `build_presentation_title` in
  `services/api/src/harrier/resume/plan.py`; the framing's lead: spec 130.
- Per-kind CV sections, templates, pages and letter limits: `KindRules` in
  `services/api/src/harrier/tracks.py`; spec 101.
- Claims, placeholders and the profile-as-evidence refusal:
  `services/api/src/harrier/apply/claims.py`; specs 065, 069.
- Dash and year checks: spec 071.
- The target shape: a resume and a letter the operator wrote by hand for
  one bounded posting on 2026-10-14. They hold personal data and are not
  in the repository.

## Out of scope

- Any change to the industry or academic voices, templates or limits.
- An entity line or links on the industry or academic templates.
- The focused documents in the browser beyond what the artifact routes
  already serve.
- A cover letter header block on the focused template.
- Verifying a link by fetching it. Links are facts the operator writes.

## Migration

The operator, once, outside the repository:

1. Adds `github` and any `links` to the stored `resume_facts`.
2. Writes the focused track's framing, with `headline`, `entity_line`,
   `ai_bullets`, `profile_lead` and `watch_terms`, then runs
   `harrier --track focused-scope profile put resume_framing --file PATH`.
3. Writes the focused application profile with `scope_reason`.
4. Adds the entity and invoicing facts the letter should state to
   `resume_truth`.

Industry and academic outputs need nothing.
