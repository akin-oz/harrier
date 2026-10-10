---
spec: 101
title: Academic application documents pass the truth and claims gates
status: accepted
approved: yes
milestone: M9
depends: [014, 034, 065, 069, 071, 074, 079, 093, 097, 098, 099, 100]
---

# Spec 101: Academic application documents pass the truth and claims gates

## Problem

An academic track finds positions (spec 097) and holds its own framing
over the shared facts (spec 099). It still cannot produce an application.
`tailor`, `cover-letter` and `answers` are not in `NON_DEFAULT_OPERATIONS`
(`services/api/src/harrier/tracks.py`), so on an academic track each exits
2. The operator writes the CV and the letter by hand, and nothing checks
them against the truth documents.

Opening the three commands as they are would be worse than refusing them:

- **The voice is industry voice, in code.** The letter prompt
  (`SYSTEM_PROMPT_BASE` in `services/api/src/harrier/apply/letters.py`)
  says the reader "is usually a recruiter or hiring manager", asks for
  "a thoughtful senior engineer" and caps the letter at a few short
  paragraphs. The answers prompt (`services/api/src/harrier/apply/answers.py`)
  is the same. The bullet ranking prompt (`SYSTEM_PROMPT_TAILOR` in
  `services/api/src/harrier/resume/ai.py`) prefers "production impact,
  reliability/observability, and technical leadership". A selection
  committee reads none of that as written for it.
- **The application profile is industry positioning.** Letters and
  answers read the `application_profile` documents
  (`services/api/src/harrier/apply/profile.py`) for positioning. There is
  one pair, shared by every track (spec 099 keeps it shared). An academic
  letter would be positioned by the industry narrative.
- **The CV has the industry shape.** `build_markdown`
  (`services/api/src/harrier/resume/markdown.py`) puts education after
  experience, and the PDF gate (`validate_rendered_pdf` in
  `services/api/src/harrier/resume/pdf.py`) requires exactly one page.

The gates themselves are not the problem. The truth gate (`TruthSources`,
spec 034, amended by spec 100) and the claims checks C1 to C10
(`services/api/src/harrier/apply/claims.py`, spec 065) read text, not
tracks. They must hold unchanged for an academic document, because a
committee reads closely and the application goes out under the
candidate's name.

## Scope

- `tailor`, `cover-letter` and `answers` run on an academic track.
- Each track kind supplies its own system prompts; the truth rules and
  return formats are shared.
- `application_profile` becomes track-owned, like the framing.
- The academic CV puts education first and may be one or two pages.
- Every truth, claims and artifact gate applies unchanged.

## Behavior

### The commands

`tailor`, `cover-letter` and `answers` join `NON_DEFAULT_OPERATIONS`.
`tailor` joins `WRITE_OPERATIONS`, because it moves the row to
`tailored_cv_requested` (spec 079), so an archived track refuses it.

On an academic track each command reads:

- the shared `resume_facts`, the shared truth documents, and the track's
  own `resume_framing` (spec 099), through `load_bundle(conn, scope)`;
- the track's own `application_profile` documents (below);
- the posting text: `--jd-file`, or the cached description, as today;
- the job's brief, if one exists. `brief set` stays refused on an academic
  track, so in practice there is none and the commands run without one,
  as they do today for a job with no brief.

They never read the default track's framing or application profile.

### Prompts by kind

`KindRules` (`services/api/src/harrier/tracks.py`) gains the kind's
prompts: the bullet ranking prompt, the letter prompt and the answers
prompt.

- **Industry:** today's three prompts, byte for byte. No industry output
  changes.
- **Academic:** new text for the voice and structure only.
  - The reader is a selection committee.
  - The letter covers research fit with the posting, what the candidate
    would bring to the group or department, and why this institution,
    from the posting's own words.
  - The bullet ranking prefers research contribution, methods, teaching,
    supervision and collaboration, wherever the bullet pool evidences
    them.

Each prompt is assembled from a shared block plus the kind's voice block.
The shared block holds every truth rule ("Return only IDs from the
supplied bullet_pool", "Every fact about the candidate must come from
resume_truth_source_md or latest_project_achievements_md", the
no-invention rules) and the return format. It is one constant per prompt,
used by both kinds, so a truth rule cannot exist in one kind and not the
other. All calls still go through the provider seam
(`services/api/src/harrier/llm/`).

### The application profile is track-owned

Spec 099's ownership rule widens by one kind. `application_profile` is
always owned by a track, like `resume_framing`. Every other kind stays
shared.

- **Migration 11** gives every existing `application_profile` row to the
  default track: `UPDATE profile_documents SET track_id = 1 WHERE kind =
  'application_profile' AND track_id IS NULL`. Content and `updated_at` are
  unchanged. If spec 103 has shipped, the migration carries the same
  statement for Postgres. Spec 090 applies it whole or not at all.
- **The readers** (`load_profile_markdown`, `load_profile_json`,
  `profile_text`) take the scope and read the scope track's documents. No
  fallback between tracks. The outreach drafts
  (`services/api/src/harrier/outreach/drafts.py`) pass the default track's
  scope, as outreach runs only there.
- A track with no application profile is refused:
  `track <slug> has no application profile; store one with harrier --track <slug> profile put application_profile --file PATH`.
- **`profile put application_profile --file PATH`** (spec 099's command,
  one more accepted kind). The format comes from the extension: `.md`
  stores `application-profile.md` as markdown, `.json` stores
  `application-profile.json` as JSON. The JSON must parse as an object,
  which is the rule `load_profile_json` already applies; the markdown must
  not be empty. Any refusal writes nothing and exits 1.

### The academic CV

`build_markdown` takes the section order from the kind.

- **Industry:** profile, selected achievements, experience, education,
  certifications, technical skills. Unchanged.
- **Academic:** profile, education, selected achievements, experience,
  certifications, technical skills.

The truth gate checks every bullet as it does today (`resolve_bullets`):
an unsupported bullet refuses the CV, naming its id (spec 034). The
required sections (`REQUIRED_SECTIONS`) are the same on both kinds.

The PDF gate takes the pages the kind allows. Industry: exactly 1, as
today. Academic: 1 or 2. Any other count fails the gate, and the
`tailor` command fails as it does today for a PDF that does not validate.
No status changes on a failed gate (spec 079).

The fit evaluation sidecar is written from the academic framing's
evaluation dimensions, as on the default track. It writes no score to the
tracker row (spec 097: no fit score on an academic track).

### The letter and the answers

- The claims checks C1 to C10 run on every academic letter and answer,
  unchanged: the same `check_claims`, with a `ClaimContext` built from the
  shared truth sources, the shared vocabulary (spec 098), the posting, and
  the track's application profile for C2's source naming (spec 069).
- An "employer" claim on an academic track is a claim about the
  institution, verified against the posting, exactly as for a company.
- The one retry after a claim refusal (spec 085) applies.
- The letter's PDF gate allows 1 or 2 pages on an academic track and 1 on
  the default track.
- The never-claim list (shared, spec 098) and the internal-label and dash
  rules (spec 071) apply to both.

### The deadline

An academic row can have a deadline (spec 093). When it has passed, each
of the three commands prints
`warning: the deadline for this call passed on <date>` to stderr and
continues. It does not refuse: some calls are reviewed on a rolling basis.

## Failure modes

- **No academic framing on the track.** `load_bundle` refuses (spec 099),
  and each command fails naming `profile put resume_framing`.
- **No application profile on the track.** `cover-letter` and `answers`
  fail naming `profile put application_profile`. `tailor` does not read it
  and runs.
- **An academic bullet the truth does not support** is selected. The CV is
  refused naming the bullet id; nothing is written to the tracker.
  `profile check` (spec 099) finds these before a run.
- **A letter claim quoting a denied line** ("lead the team" from "I didn't
  lead the team"). C2 refuses it once spec 100 is implemented. Before
  that, the gate reads negation as it does today on every track.
- **The model writes in industry voice anyway.** No check reads voice. The
  operator reads the draft; nothing auto-sends (product invariant).
- **A three-page academic CV.** The PDF gate fails with today's message
  naming the page count and the allowed counts.
- **The default track after migration 11.** Its application profile is
  owned by it, so every default-track letter, answer and outreach draft
  reads the same documents as before. No output changes.
- **`--track <academic slug> evaluate`, `outreach-draft`, `find-contacts`,
  `brief set`.** Still refused, exit 2 (spec 093, spec 097).

## Acceptance criteria

- [x] On a synthetic academic track with its own framing, `tailor` writes
      a CV whose education section comes before achievements, verifies
      every bullet against the shared truth, accepts a 2-page PDF and
      moves the row to `tailored_cv_requested`
      (`services/api/tests/test_academic_applications.py::test_academic_tailor_uses_the_track_framing_and_shape`).
- [x] The letter and the answers on an academic track never read the
      default track's application profile: every prompt payload holds the
      track's own and not the default track's
      (`services/api/tests/test_academic_applications.py::test_academic_documents_read_only_their_track`). The
      framing is read by track (spec 099,
      `services/api/tests/test_track_framing.py::test_a_track_reads_only_its_own_framing`).
- [x] An academic CV selecting an unsupported bullet is refused naming the
      id, and the row's status is unchanged
      (`services/api/tests/test_academic_applications.py::test_academic_cv_refuses_an_unsupported_bullet`).
- [x] `cover-letter` and `answers` on the academic track run `check_claims`
      with the track's profile; a letter quoting a fragment outside the
      truth documents is refused, as on the default track
      (`services/api/tests/test_academic_applications.py::test_academic_letters_pass_the_same_claims_checks`); a
      track with no application profile is refused
      (`services/api/tests/test_academic_applications.py::test_a_track_with_no_application_profile_is_refused`).
- [x] The industry prompts are byte-identical to today's, and every
      prompt of both kinds contains its shared truth-rule block
      (`services/api/tests/test_academic_documents.py::test_both_kinds_share_every_truth_rule`).
- [x] Migration 12 owns every `application_profile` row by the default
      track with content and `updated_at` unchanged, and the default track
      reads them as before
      (`services/api/tests/test_academic_applications.py::test_migration_12_owns_the_application_profile`;
      on Postgres, per owner, `test_migration_12_owns_the_application_profile_on_postgres`).
- [x] `profile put application_profile --file` stores `.md` and `.json`
      under their names on the scope's track, and refuses an empty
      markdown file, JSON that is not an object, and any other extension
      (`services/api/tests/test_academic_applications.py::test_profile_put_stores_an_application_profile`).
- [x] A 3-page academic PDF and a 2-page industry PDF each fail the gate
      (`services/api/tests/test_academic_documents.py::test_page_gate_reads_the_kind`).
- [x] A passed deadline prints the warning and the command still runs
      (`services/api/tests/test_academic_documents.py::test_a_passed_deadline_warns_and_continues`).
- [x] `tailor` on the default track's demo job produces the same markdown
      as before this change
      (`services/api/tests/test_academic_documents.py::test_industry_tailor_is_unchanged_by_kind_prompts`).
- [x] `test_academic_commands_read_no_profile_document` passes with
      `tailor`, `cover-letter` and `answers` added to its exceptions
      (`services/api/tests/test_tracks_cli.py::test_academic_commands_read_no_profile_document`).
- [x] `config/resume-framing.academic.example.json` and
      `config/application-profile.academic.example.md` are synthetic,
      classified public; their real names are never-in-git in
      `config/data-classification.json`, `.gitignore` and `.dockerignore`.
      The two examples and `config/application-profile.academic.example.json`
      store and check as a valid track
      (`services/api/tests/test_academic_applications.py::test_the_academic_examples_store_and_check`).
- [x] `uv run ruff check`, `uv run pyright`, `just contract` and
      `just check` pass. The contract diff is the 409 response on the apply
      routes (Amendment).

## Honest limitations

- **No publications, teaching record or grants.** The CV shows what the
  facts hold (spec 098). An academic CV without a publication list is
  incomplete for most calls, and adding it needs new facts and a later
  spec.
- **No research statement, teaching statement or reference letters.**
  Only the CV, the letter and the answers.
- **Voice is unchecked.** The gates check truth and claims. Whether a
  letter reads as written for a committee is the prompt's job and the
  operator's review.
- **Two pages is a ceiling, not a convention.** Some calls ask for longer
  CVs; the gate refuses them until the allowed counts change.
- **The browser does not offer these commands on an academic track** until
  the browser specs place them.

## Migration

Migration 11 runs on the next open (`just container-up`), after
migration 10 (spec 099). Then, for each academic track:

1. Write an application profile from
   `config/application-profile.academic.example.md` and store it with
   `harrier --track <slug> profile put application_profile --file PATH`.
2. If letters or answers need the JSON half, store it the same way from a
   `.json` file.
3. `harrier --track <slug> profile check`, then
   `harrier --track <slug> tailor <job>` on one call and read the CV.

## Options weighed

- **Only the CV in this spec.** Smaller, but the letter is where an
  academic application is decided, and the claims checks are already
  built. Declined (Akin, 2026-10-10).
- **No application profile on an academic track.** No ownership change,
  but the letter loses its positioning input. Declined (Akin, 2026-10-10).
- **The industry prompts for both kinds.** A recruiter voice to a
  committee. Declined (Akin, 2026-10-10).
- **The industry CV shape and the one-page gate.** Declined (Akin,
  2026-10-10).
- **Refusing a run after the deadline.** Would block rolling calls.
  Declined in favour of a warning.

## Decisions recorded (Akin, 2026-10-10)

Approved as written, so each open decision keeps the behavior the spec
states:

1. **The academic section order** is profile, education, selected
   achievements, experience, certifications, technical skills.
2. **The academic letter's length** is one to two pages, with no word
   count.
3. **A passed deadline warns** and the command continues.

## Proof / origin

- The track rules: `services/api/src/harrier/tracks.py` (`KindRules`,
  `NON_DEFAULT_OPERATIONS`, `WRITE_OPERATIONS`); specs 093, 097.
- The prompts: `services/api/src/harrier/resume/ai.py`
  (`SYSTEM_PROMPT_TAILOR`), `services/api/src/harrier/apply/letters.py`
  and `services/api/src/harrier/apply/answers.py` (`SYSTEM_PROMPT_BASE`).
- The application profile readers: `services/api/src/harrier/apply/profile.py`;
  the outreach reader: `services/api/src/harrier/outreach/drafts.py`.
- The CV: `services/api/src/harrier/resume/markdown.py` (`build_markdown`,
  `resolve_bullets`, `REQUIRED_SECTIONS`); the PDF gate:
  `services/api/src/harrier/resume/pdf.py` (`validate_rendered_pdf`);
  the run: `services/api/src/harrier/resume/tailor.py`.
- The claims checks: `services/api/src/harrier/apply/claims.py`
  (`check_claims`, `ClaimContext`); specs 065, 069, 085, 087.
- The truth gate: spec 034, amended by spec 100.
- Ownership and the `put` command: spec 099.
- The follow-on list this spec closes: spec 097, Out of scope.

## Out of scope

- Publications, teaching, grants and the facts they need.
- Research statements, teaching statements and reference requests.
- Outreach, contact discovery and the offer evaluation (`evaluate`) on an
  academic track.
- `brief set` and `brief show` on an academic track.
- A fit score or ranking for academic rows (spec 097).
- Running these commands from the browser on an academic track.
- Owning any kind other than `resume_framing` and `application_profile`.
- Changes to the truth gate (spec 100) or to the claims rules themselves.

## Amendment (2026-10-10, during implementation)

Three gaps, found while building the prompts, the CV shape and the page
gate. Each is a consequence of behavior the spec already states.

- **The PDF's section order is the template's.** `render_html`
  (`services/api/src/harrier/resume/htmlrender.py`) fills fixed slots in
  `templates/resume-template.html`, where education sits in the footer.
  Reordering only the markdown would leave the PDF a committee reads in
  the industry order. `KindRules.cv_template` names the template:
  industry keeps `resume-template.html` unchanged, academic uses
  `templates/resume-template-academic.html`, with the sections in the
  academic order and the same stylesheet. Proved by
  `services/api/tests/test_academic_documents.py::test_the_academic_cv_puts_education_before_achievements`.
- **The letter check held three paragraphs and 240 words on every
  letter.** `cover_letter_violations`
  (`services/api/src/harrier/apply/letters.py`) refused any letter outside
  that shape, so an academic letter of one to two pages with no word count
  (decision 2) could never pass. The paragraph count and word cap now come
  from the kind (`KindRules.letter_paragraphs`, `letter_max_words`):
  three and 240 on the industry kind as before, none on the academic,
  where the 1 or 2 page gate holds the length. A brief's stated limits
  still replace either. The eight-word floor on a paragraph, the banned
  phrasing and the bullet-list rule hold for both. Proved by
  `services/api/tests/test_academic_documents.py::test_the_academic_letter_has_no_word_count_or_paragraph_count`.
- **A shared block is more than one segment.** The industry prompts carry
  truth rules between voice rules (the letter's "Every fact about the
  candidate" line sits among its length rules), so a byte-identical
  industry prompt cannot be one voice block followed by one shared block.
  Each prompt's shared part is one constant, a tuple of segments
  (`TAILOR_SHARED`, `LETTER_SHARED`, `ANSWERS_SHARED`), and each kind's
  voice (`services/api/src/harrier/voices.py`) is the segments between
  them. A truth rule still exists in one place, used by both kinds.
  `KindRules` holds the voices, and `tailor_prompt`, `letter_prompt` and
  `answers_prompt` assemble a kind's prompt from its voice and the shared
  constant, each taking the kind explicitly. Proved by
  `services/api/tests/test_academic_documents.py::test_both_kinds_share_every_truth_rule`.

## Amendment (2026-10-10, wiring after spec 099)

- **Migration 11 is migration 12.** Spec 099 took 11. Every "migration 11"
  above means migration 12. On Postgres each owner already has a track 1
  (spec 105), so the same `UPDATE` gives each owner's application profile
  to that owner's own default track.
- **The browser apply routes refuse an academic track.** The CLI and the
  API share one allowlist (spec 094), so adding the three commands to it
  opened the browser's `/apply/{selector}/resume`, `cover-letter` and
  `answers` too, which this spec keeps out of scope. Each now refuses a
  non-default track with 409 before anything is staged, as discovery's
  route does (spec 095). The contract gains that 409 response
  (`services/api/tests/test_api_tracks.py::test_the_apply_routes_refuse_another_track`).
- **The generators take the scope, not the kind.** `generate_cover_letter`,
  `generate_answer_set` and their payload builders read the kind from the
  scope, so the prompt and the application profile always come from the
  same track. The outreach drafts pass the default track's scope.
- **`profile import` gives owned kinds to the default track.** The old
  repository had one search, and the write path refuses a shared
  application profile now.
- **An academic application profile JSON example.** Letters and answers
  read both halves of the profile, so `config/application-profile.academic.example.json`
  joins the markdown example, with its real name never-in-git.
- **The trace callback became a payload check.** The letter and answers
  tests prove the default track's profile is never read by putting a
  different sentence in each track's profile and finding only the track's
  own in every prompt payload.
