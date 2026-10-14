---
spec: 137
title: A focused track scores bounded roles by its own configuration
status: accepted
approved: yes
milestone: M9
depends: [033, 091, 092, 093, 097, 099, 112]
---

# Spec 137: A focused track scores bounded roles by its own configuration

## Problem

The industry track is one search with one candidate configuration. It
reads every posting as a step toward a senior role with broad ownership:
the title rules, the keyword bonuses and the resume framing all assume it.

The operator now runs a second search with the same facts and a different
target. It looks for roles with a bounded scope: a junior or mid frontend
role that implements designs, ships interface work and does not ask for
broad ownership. The split between the two searches is the ownership a
posting demands, not whether it is a contract or an employment. A junior
frontend posting scores well on the industry configuration today because
its stack matches. The industry resume and letter then present a senior
engineer looking for ownership, which is the wrong story for that posting.

Three things in the code stop a second search of this kind:

- **A second industry track is refused.** `kind_refusal` in
  `services/api/src/harrier/tracks.py` says it "would share the one
  candidate configuration and watchlist with the first".
- **The only other kind is academic.** It brings the deadline queue, the
  academic gates (spec 097) and the academic voices (spec 101). None of
  them fit a frontend role.
- **No document can hold a second screening configuration.** The scorer
  reads the shared `candidate` document
  (`harrier.screening.config.load_candidate_config`). `TRACK_OWNED_KINDS`
  in `services/api/src/harrier/profile/store.py` is `resume_framing` and
  `application_profile` only.

The person affected is the operator. Today they can only keep bounded
roles in the industry track and fix each document by hand.

This spec adds the kind, its configuration and its score on `add`. Spec
138 moves a row between tracks. Spec 139 places each discovered posting by
its ownership demand. Spec 140 gives the kind its documents.

## Scope

- `services/api/src/harrier/tracks.py`: `focused` in `TRACK_KINDS`, its
  `KIND_RULES` entry, and `kind_refusal` allowing one unarchived focused
  track.
- `services/api/src/harrier/tracker/schema.py`: one migration, in both
  dialects (spec 112), widening the `tracks.kind` CHECK. A guarded path;
  approved under this spec.
- `services/api/src/harrier/profile/store.py`: `screening` joins
  `TRACK_OWNED_KINDS`, ownable by a focused track only.
- `services/api/src/harrier/screening/focused.py` (new): the screening
  document's parser and the focused score.
- `services/api/src/harrier/capture.py`: `add` on a focused track scores
  with the track's screening document.
- `services/api/src/harrier_cli/main.py`: `profile put screening`, and the
  per-kind refusals below.
- `services/api/src/harrier_api/tracks_routes.py`: the browser's kind list
  leaves the focused kind out (amendment 3).
- `services/api/src/harrier_api/capture_routes.py`: a capture on a focused
  track with no screening document answers 409 (amendment 4).
- `services/api/src/harrier/screening/policy.py`: the rule fingerprint the
  focused version digests becomes public (amendment 4).
- `config/data-classification.json`, `.gitignore` and `.dockerignore`: the
  example's real name, `config/screening.focused.json`, is never-in-git and
  never in the image (amendment 5). A guarded path; approved under this
  spec.
- `config/screening.focused.example.json` (new, public, synthetic): the
  document's shape and the proposed defaults.
- `.ai/rules/product-invariants.md`: the remote-only sentence names the
  focused kind too. Edited on the implementing branch, then `npx aie sync`
  regenerates `CLAUDE.md`, `AGENTS.md` and `.claude/`.
- Tests: `services/api/tests/test_focused_track.py` (new), plus additions
  to `services/api/tests/test_tracks_cli.py`.

## Behavior

### The kind

`harrier tracks add <slug> --kind focused --label <label>` creates a
focused track. A second unarchived focused track is refused with exit 2:
one install has one bounded-role search, the same way it has one industry
search. An archived focused track does not count.

`KIND_RULES["focused"]` reuses the industry labels, next actions,
follow-up seeding and `stage_then_score` queue. Status labels stay the six
shared statuses (product invariant "One tracker, one write path").

### The screening document

A focused track owns one profile document of kind `screening`, written by
`harrier --track <slug> profile put screening --file PATH`. The write
validates before it stores anything. It is refused with exit 2 on any
track that is not focused.

The document is JSON:

```json
{
  "targets": {
    "titles": ["..."],
    "title_keywords_include": ["..."],
    "title_keywords_exclude": ["..."]
  },
  "scoring": {
    "base_score": 0,
    "exact_title_bonus": 0,
    "include_keyword_bonus": 0,
    "include_keyword_bonus_cap": 0,
    "remote_bonus": 0,
    "preferred_region_bonus": 0
  },
  "bonuses": [
    {"name": "...", "phrases": ["..."], "fields": ["title", "description"], "points": 0}
  ],
  "penalties": [
    {"name": "...", "phrases": ["..."], "fields": ["title"], "points": 0}
  ],
  "ownership": {
    "broad_title_signals": ["..."],
    "broad_description_signals": ["..."]
  }
}
```

`ownership` is stored and validated here and read by spec 139. Every list
holds non-empty strings. `points` is a positive integer. `fields` is a
non-empty subset of `title` and `description`. An unknown key is refused.

### The focused score

On a focused track, `add` (and spec 139's discovery) runs:

1. **The industry gates, unchanged.** The title rule over the screening
   document's targets, then remote-only and EMEA scope on location signals,
   as `harrier.screening.rules` applies them, with the regions of the
   shared candidate configuration. A manual `add` lands whatever its gates
   say, as it does on the industry track, and the first failed gate is
   named in the row's `signals` as `gate=<reason>` (amendment 1). The gates
   filter discovery (spec 139).
2. **The score.** `base_score`, plus `exact_title_bonus` on an exact title
   match, plus `include_keyword_bonus` per title keyword found in the title
   up to its cap, plus `remote_bonus` and `preferred_region_bonus` as the
   industry score applies them. Then each bonus group adds its `points`
   once if any of its phrases appears in its fields. Each penalty group
   subtracts its `points` once if any of its phrases appears in its fields.
   The result is floored at 0. Phrases and title keywords match
   case-insensitively on whole words.
3. **Blockers.** A posting with a blocker (specs 078, 081, 088) loses the
   blocker penalty derived from the screening document, so it ranks below
   every posting without one, as on the industry track. Its `signals`
   names each blocker (amendment 2).

Penalties rank and never reject. The industry rule that keeps negative
hints to the location field governs rejection. It is not changed, and a
penalty phrase in a description never removes a posting.

The row's `signals` names each group that fired, as
`scorer=focused|bonus=<name>|penalty=<name>`, followed by any
`blocker=<kind> "<phrase>"` and `gate=<reason>`. `scoring_version` is the
policy digest of the screening document, so a changed weight changes the
version (spec 079).

### What a focused track may run

`NON_DEFAULT_OPERATIONS` is unchanged. Three of its operations are refused
on a focused track with exit 2 and a message naming the spec that opens
them:

- `discover`, `reconsider`: spec 139 (`reconsider` by amendment 6).
- `tailor`, `cover-letter`, `answers`: spec 140.

`profile put screening` joins the allowed operations and the write
operations.

### Proposed defaults

`config/screening.focused.example.json` carries these weights for the
owner to confirm. They are proposals, not measured values.

- `titles`: Frontend Engineer, Frontend Developer, React Developer,
  Next.js Developer.
- `title_keywords_include`: frontend, front-end, react, next.js,
  typescript, ui.
- `title_keywords_exclude`: manager, head of, director, vp.
- `scoring`: base 30, exact title 20, include keyword 6 capped at 18,
  remote 10, preferred region 8 (the industry example's values).
- Bonuses: `figma` (figma, design handoff, pixel) 8 on description;
  `accessibility` (accessibility, a11y, wcag) 6 on description;
  `performance` (performance, core web vitals, lighthouse) 6 on
  description; `level` (junior, mid, medior, intermediate) 8 on title;
  `bounded-time` (part-time, 4 days, four days, 32 hours, freelance,
  contract, project-based) 6 on description.
- Penalties: `seniority` (staff, principal, lead, head) 25 on title;
  `on-call` (on-call, on call, pager duty) 15 on description;
  `ownership` (end-to-end ownership, own the roadmap, full ownership,
  technical leadership) 12 on description; `full-week` (5 days a week,
  five days a week, 40 hours) 8 on description; `us-hours` (pst, est, us
  hours, us time zone) 15 on description.
- `ownership.broad_title_signals`: staff, principal, lead, head,
  architect. "Senior" is not a signal: the title alone does not say how
  much ownership a role demands, and the description signals decide
  (owner's decision, 2026-10-14).
- `ownership.broad_description_signals`: end-to-end ownership, own the
  roadmap, technical leadership, lead a team, on-call.

## Failure modes

- **A second focused track.** `tracks add --kind focused` exits 2 naming
  the existing one. The database is not touched. Two adds that race past
  the check are refused by the store: migration 20 adds a partial unique
  index on the unarchived focused track (amendment 7).
- **No screening document.** `add` on a focused track exits 2 with
  "track <slug> has no screening configuration; `harrier --track <slug>
  profile put screening --file PATH`". It never falls back to the
  committed example: a demo weight would rank real rows.
- **A malformed document.** `profile put screening` exits 2 naming the
  first bad field path, such as `penalties[2].points`, and stores nothing.
- **A screening document on a non-focused track.** Exit 2 from the CLI;
  `put_document` raises for any other caller.
- **The migration on a populated database.** SQLite cannot alter a CHECK,
  so `tracks` is rebuilt in one transaction (spec 090). Every track keeps
  its id, slug, kind, label, `created_at` and `archived_at`. The
  `profile_documents.track_id` reference and the `jobs` track triggers
  still resolve. On Postgres the CHECK is replaced in place.
- **An archived focused track.** `add` and `profile put screening` exit 2
  naming the track as archived (spec 093).

## Acceptance criteria

- [ ] `harrier tracks add f --kind focused --label F` then `tracks list`
  prints the new track with kind `focused` (`services/api/tests/test_focused_track.py::test_a_focused_track_can_be_added`).
- [ ] A second unarchived focused track exits 2 and adds no row
  (`services/api/tests/test_focused_track.py::test_a_second_focused_track_is_refused`),
  and two racing adds leave one (amendment 7;
  `services/api/tests/test_focused_track.py::test_two_racing_focused_adds_leave_one`).
- [ ] A posting with the title "Junior Frontend Developer", a remote EMEA
  location and "Figma" in its description is added to a focused track with
  a score that includes the `level` and `figma` bonuses, and its `signals`
  names both (`services/api/tests/test_focused_track.py::test_focused_add_scores_with_the_tracks_document`).
- [ ] The same posting with "on-call rotation" in its description scores
  15 lower and is still added (`services/api/tests/test_focused_track.py::test_a_penalty_ranks_and_never_rejects`).
- [ ] A posting with an onsite location is added to a focused track, and
  its `signals` names the industry gate's reason (amendment 1;
  `services/api/tests/test_focused_track.py::test_focused_add_names_a_failed_gate`).
- [ ] A posting with a blocker scores below the least an unblocked posting
  can score, and its signals name the blocker (amendment 2;
  `services/api/tests/test_focused_track.py::test_a_blocked_posting_ranks_below_every_unblocked_one`).
- [ ] The browser's kind list does not offer the focused kind, and a track
  of that kind still lists over the API (amendment 3;
  `services/api/tests/test_tracks_cli.py::test_the_browser_cannot_add_a_focused_track`).
- [ ] `add` on a focused track with no screening document exits 2 and adds
  no row (`services/api/tests/test_focused_track.py::test_focused_add_without_configuration_is_refused`).
- [ ] `profile put screening` with a negative `points` exits 2 naming the
  field and stores nothing (`services/api/tests/test_focused_track.py::test_a_malformed_screening_document_is_refused`).
- [ ] `profile put screening` on the industry track exits 2 (`services/api/tests/test_focused_track.py::test_screening_is_owned_by_a_focused_track_only`).
- [ ] `discover`, `reconsider`, `tailor`, `cover-letter` and `answers` on a
  focused track exit 2 naming specs 139 and 140 (`services/api/tests/test_focused_track.py::test_focused_refuses_what_later_specs_open`).
- [ ] The migration on a database holding an academic and an archived
  track keeps every track row equal, in both dialects (`services/api/tests/test_focused_track.py::test_the_kind_migration_keeps_every_track`).
- [ ] With a focused track present, every industry-track command prints
  what it printed before, and an industry `add` stores the same score and
  `scoring_version` (`services/api/tests/test_focused_track.py::test_the_industry_track_is_unchanged_by_a_focused_track`).
- [ ] `just check` passes.

## Amendments found while implementing (2026-10-14)

Each was put to the owner and answered before it was built.

1. **A manual add is not refused by a gate.** This spec said a focused
   `add` refuses a posting that fails a gate "as on the industry track".
   The industry track does no such thing: `add_captured_job`
   (`services/api/src/harrier/capture.py`) lands every manual add as a
   prospect, because a person chose it. The owner chose to match the
   industry track. The focused `add` lands the row and names the first
   failed gate in `signals`. The acceptance criterion for an onsite posting
   changed with it.
2. **Blockers rank last on the focused track too.** The industry score
   subtracts a derived penalty for a blocker (specs 078, 081). The formula
   above left it out, so a posting the candidate cannot take could rank
   high. The owner chose to include it.
3. **The browser keeps its two kinds.** `GET /tracks/kinds` listed every
   kind in `TRACK_KINDS`, so adding the focused kind would have offered it
   in the browser's add form, which Out of scope rules out. The route now
   lists every kind but the focused one. `TrackIn.kind` already refused it,
   and `TrackOut.kind` is a string, so the API contract does not change.
4. **Two seams the scope list missed.** The capture routes map each capture
   status to an HTTP status; the new `unconfigured` status maps to 409,
   which the routes already declare. The focused policy version digests the
   same rule tables as the industry one, through `rule_fingerprint` in
   `services/api/src/harrier/screening/policy.py`, renamed from
   `_rule_fingerprint` so a second module can read it.

5. **The example's real name is classified.** Every `config/*.example.*`
   names a file an operator may create beside it, and
   `services/api/tests/test_classification_coverage.py::test_every_example_config_has_its_real_name_classified`
   requires that name to be never-in-git. `config/screening.focused.json`
   joins the never-in-git list, `.gitignore` and `.dockerignore`, as
   `config/academic-searches.json` did under spec 097
   (`services/api/tests/test_container.py::test_every_never_in_git_path_is_kept_out_of_the_image`
   holds the image to the list). The Migration
   section's advice to keep the copy outside the repository still holds;
   this stops a copy made beside the example from being staged.

6. **`reconsider` is held with `discover`.** The local review of PR #332
   found that `reconsider` passes a focused track's allowlist and falls
   through to the industry path: it read the industry seen state and the
   focused track's rows for human rejections, so it cleared an industry
   rejection a person had made. Reconsideration reopens what discovery
   rejected, and discovery on the kind is spec 139's, so `reconsider` is
   refused on the kind until then
   (`services/api/tests/test_focused_track.py::test_focused_refuses_what_later_specs_open`).

7. **The store holds one unarchived focused track.** The local review and
   CodeRabbit's review of PR #332 both found that `add_track` checks before
   it inserts, so two adds with different slugs can both pass the check and
   both commit; Postgres reads at read committed. Migration 20 now also
   creates `idx_tracks_one_active_focused`, a partial unique index on the
   unarchived focused track (per owner on Postgres). A second insert the
   index refuses is reported as the focused-track refusal, and a slug clash
   stays a duplicate
   (`services/api/tests/test_focused_track.py::test_two_racing_focused_adds_leave_one`,
   on both stores).
8. **The version digests the parsed document.** CodeRabbit's review of PR
   #332 found that the policy version hashed the raw document while the
   parser strips surrounding spaces, so a whitespace-only edit moved the
   version of the same policy. It now digests the parsed values
   (`services/api/tests/test_focused_track.py::test_a_whitespace_edit_keeps_the_policy_version`).

The migration's order is recorded in its comment in
`services/api/src/harrier/tracker/schema.py`. A rebuild into a new table
and a rename, the usual shape, fails on SQLite twice: the rename on the
`jobs` triggers that name `tracks`, and the commit on the foreign key
violations the drop deferred.

## Proof / origin

- The refusal this spec lifts for one new kind: `kind_refusal` and
  `add_track` in `services/api/src/harrier/tracks.py`; spec 093.
- The kind table and the per-kind operations: `KindRules`,
  `NON_DEFAULT_OPERATIONS`, `WRITE_OPERATIONS` in the same file; specs 093,
  097, 101.
- The industry gates and score this kind reuses:
  `services/api/src/harrier/screening/rules.py` (`title_allowed`,
  `score_job`), the location-only scoping of negative hints pinned by
  `services/api/tests/test_screening.py`; spec 033 removed the cutoff.
- Owned documents: `TRACK_OWNED_KINDS` in
  `services/api/src/harrier/profile/store.py`; spec 099.
- The operator's request of 2026-10-14: a second search split by the
  ownership a posting demands, not by contract type.

## Out of scope

- Discovery into a focused track: spec 139.
- Moving a row between tracks: spec 138.
- Resume, letter and answers on a focused track: spec 140.
- A pay floor. Postings rarely state pay and harrier parses none. Recorded
  for a later spec if the operator wants one.
- Outreach, contacts and the digest on a focused track.
- The API and the browser: no route accepts the `focused` kind until a
  later spec. The browser's add-track form keeps its two kinds.
- More than one focused track.

## Migration

The operator adds the track and its configuration once:

1. `harrier tracks add focused-scope --kind focused --label "Focused scope"`
2. Copy `config/screening.focused.example.json` outside the repository,
   edit it, then run
   `harrier --track focused-scope profile put screening --file PATH`.

Existing tracks and rows need nothing.
