---
spec: 071
title: The tailored resume leads with what the posting needs, keeps canonical titles and years, and writes no dashes as punctuation
status: accepted
approved: yes
milestone: M8
depends: [013, 062, 063, 066, 070]
---

# Spec 071: The tailored resume leads with what the posting needs, keeps canonical titles and years, and writes no dashes as punctuation

## Problem

The resume from the same 2026-10-03 run as spec 070 had five defects of
its own.

1. **Dashes as punctuation.** The header title joins the identity and its
   highlighted technologies with an em dash (`build_presentation_title`,
   `services/api/src/harrier/resume/plan.py`). Every role heading joins
   organization and title with an em dash (`TITLE_SEPARATOR` in
   `services/api/src/harrier/resume/heading.py`, spec 063). Every period
   joins start and end with an en dash (`role_period_label` in `facts.py`).
   The operator does not send text with these marks.
2. **Achievement order ignores the posting's core requirements.**
   Achievements are ranked by keyword score, then reordered by the model.
   An achievement that is Direct evidence for a must-have can sit below one
   that matches a nice-to-have.
3. **Dated achievements stay on top.** An achievement built on one
   framework is shown first for a posting that asks for its competitor.
4. **Nothing stops a seniority title the record does not hold.** The
   presentation title comes from the bundle today, but no check refuses a
   staff or principal title, and the requested role for this run was
   "Senior/Principal".
5. **Experience wording is fixed in code.** The profile says
   `<N>+ years of professional experience`. The operator's exact wording
   cannot be expressed, and no check stops a larger year count or "a
   decade" reaching the text.

A sixth need has no home: a skill the candidate has but the CV omits, now
confirmed for this application in answer to a spec 070 question.

## Scope

`plan.py`, `markdown.py`, `facts.py`, `heading.py`, `htmlrender.py`, the
bundle parser in `content.py`, the brief parser in
`services/api/src/harrier/apply/brief.py`, and the governance source
`.ai/rules/product-invariants.md`. The evaluation report is spec 070.

## Behavior

| # | Behavior |
|---|---|
| O1 | **No dashes as punctuation in the resume.** The rendered markdown, HTML and PDF text contain no em dash (U+2014), no en dash (U+2013), no `--`, and no hyphen with a space on both sides. A hyphen inside a word (`Full-Stack`, `vertical-slice`) and a markdown list marker at line start are allowed. |
| O2 | **New separators.** The header title is `<identity>, <tech> & <tech>`. The role heading separator `TITLE_SEPARATOR` becomes `", "`, so a heading reads `<organization>, <title> (<type>)`; the writer, the HTML parser and the bundle validator move together as spec 063 requires. A period reads `<Mon YYYY> to <Mon YYYY>` or `<Mon YYYY> to Present`. |
| O3 | **Bundle text with a dash is refused by name.** A bundle string that the resume emits and that contains a mark O1 forbids is refused at parse time, naming the field path, as spec 062 refuses line breaks. An organization containing `", "` is refused by the existing separator rule, now naming the comma. |
| O4 | **Core-first achievements.** After any model ordering, Selected Achievements are stably sorted by: the number of `core` requirements (spec 070 X7) the achievement is Direct evidence for, then Partial, both descending. The model's order breaks ties. |
| O5 | **Dated achievements demoted.** An achievement naming a technology whose vocabulary family (spec 070 `vocabulary.py`, for example frontend frameworks: React, Vue, Angular, Svelte) has another member named by the posting, while the achievement names no technology the posting names, sorts after all others. With four slots, it is dropped when four others qualify. |
| O6 | **Titles are canonical.** Role titles render exactly as in the bundle. `validate_content_plan` refuses a presentation title containing staff, principal, lead or head unless `primary_identity` or a bundle role title contains that word. The requested role never contributes a word to the title. |
| O7 | **Experience wording from the bundle.** The bundle gains an optional `experience_statement` string. When present, the profile uses it in place of the derived `<N>+ years of professional experience` phrase. It is refused at parse time when it contains a year count larger than the computed one (`professional_experience_years`). |
| O8 | **No year count beyond the record.** The rendered resume is refused when it contains `<N>+ years` or `<N> years` with N above the computed professional years, or the words `decade` or `decades`. The operator's own phrase list (`forbidden_phrases`) still applies on top. |
| O9 | **Confirmed skills.** The application brief (spec 066) gains an optional `confirmed_skills` list. A skill in it enters the resume's skills for that job only when it is also in the bundle's `all_skills` or a line of the truth source. A confirmed skill in neither is refused when the brief is set, naming the skill. Skills not confirmed follow today's `verified_skills` rule. |
| O10 | **Plain generated prose.** Sentences the code writes (the profile lead) contain none of: may, might, perhaps, possibly, somewhat, fairly, relatively, arguably. Bullets are verified text and are not rewritten. |
| O11 | **The rule is governance too.** `.ai/rules/product-invariants.md` gains, under Artifact gates: generated resume and letter text carries no em dash, en dash or double hyphen, and no spaced hyphen as punctuation (spec 071). `npx aie sync` regenerates `CLAUDE.md` and `AGENTS.md`; neither is hand-edited. |

## Acceptance criteria

Tests in `services/api/tests/test_resume.py` unless named otherwise, on the
synthetic bundle and the spec 070 Weflow fixture.

| Criterion | Proof |
|---|---|
| O1 generated CV text has no forbidden dash | test_generated_resume_text_has_no_dash_punctuation, over markdown and HTML for the Weflow fixture and a React posting |
| O1 the check itself fails on each mark | test_dash_check_catches_each_mark, parametrized over em dash, en dash, `--`, spaced hyphen |
| O2 title, heading and period formats | test_title_uses_comma_separator, test_role_heading_uses_comma_separator_and_splits_back, test_period_reads_to_present |
| O3 a bundle bullet with an em dash is refused by path | test_bundle_string_with_dash_punctuation_is_refused_by_name |
| O4 core Direct achievements lead | test_achievements_ordered_by_core_requirement_evidence |
| O4 model order cannot move a core achievement down | test_ai_order_breaks_ties_only |
| O5 a competing-framework achievement is demoted | test_vue_achievement_demoted_for_react_posting |
| O6 a principal requested role never yields a principal title | test_principal_requested_role_keeps_canonical_title |
| O6 a staff title without a record is refused | test_plan_refuses_unbacked_seniority_title |
| O7 the statement replaces the derived phrase | test_experience_statement_replaces_derived_phrase |
| O7 a statement claiming more years is refused | test_experience_statement_with_larger_years_is_refused |
| O8 "10+ years" and "a decade" are refused | test_rendered_resume_refuses_inflated_years, parametrized |
| O9 a confirmed skill in all_skills enters for that job only | test_confirmed_skill_enters_skills_for_its_job_only (in `test_apply_brief.py`) |
| O9 a confirmed skill outside every source is refused on set | test_confirmed_skill_without_source_is_refused (in `test_apply_brief.py`) |
| O10 the profile lead has no hedge word | test_profile_lead_has_no_hedge_words |

- [ ] the spec 063 tests move with the separator and stay green
- [ ] spec 063's text is amended to name `", "` as the separator
- [ ] `config/resume-content.example.json` and
      `config/application-brief.example.json` show the new optional keys
      with synthetic values
- [ ] each criterion's test fails when its behavior is removed
- [ ] `npx aie check` and `just check` green

## Data and privacy

The operator's own experience wording goes in their local bundle, never in
code, tests or this spec. Tests use a synthetic statement. The brief's
`confirmed_skills` is personal data and lives in the database like the
rest of the brief.

## Limitations

- **"Dated" is a family rule.** An achievement that reads dated for
  another reason (an old practice, an old metric) is not caught.
- **Hedge words are a list.** Hedging phrased another way passes.
- **Existing bundles with dashes stop rendering** until the operator edits
  the offending strings. O3 names each one. This is deliberate: silently
  rewriting verified text would break the truth match.
- **A comma in an organization name** is now refused (O3). An organization
  with a legal suffix such as "Example, Inc." must drop the comma in the
  bundle.

## Proof / origin

`build_presentation_title` and `build_profile` in `plan.py`,
`role_period_label` in `facts.py`, `TITLE_SEPARATOR` in `heading.py`, all
as of commit d5cda0e. The operator's request of 2026-10-04 (items D1 to D7).

## Out of scope

The evaluation report (spec 070). Cover letters and answers, except that
O11's governance line covers them; enforcing O1 in the letter path is its
own change. A GUI for confirmed skills.
