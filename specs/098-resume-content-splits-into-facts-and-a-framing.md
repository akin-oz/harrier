---
spec: 098
title: Resume content splits into shared facts and an industry framing, on the default track
status: proposed
approved: no
milestone: M9
depends: [004, 013, 034, 059, 062, 065, 071, 074, 091, 093, 097]
---

# Spec 098: Resume content splits into shared facts and an industry framing, on the default track

## Problem

The resume content bundle is one profile document: kind `resume_data`, read
by `load_bundle` (`services/api/src/harrier/resume/content.py`). It holds
two different things in one object.

- **Facts about the candidate.** Name and contact fields, the career start,
  each role's organization, title, employment type, period and
  technologies, education, certifications, the skill vocabulary, and the
  never-claim list (spec 034). These are true or false. They do not change
  with the kind of job applied for.
- **How the industry resume presents them.** `primary_identity`,
  `positioning_technologies`, `profile_summary`, `experience_statement`, the
  bullet pool and its evidence groups, each role's competencies, bullet
  count and default bullets, the target signal weights, the evaluation
  dimensions and the default achievements. These are choices made for
  software industry applications.

Spec 097 gave an academic track its own discovery. Spec 099 will give it
its own presentation: a research identity, research-worded bullets,
different dimensions. With one document there are two ways to do that, and
both are wrong:

- **A second full copy for the academic track.** Every fact is then held
  twice. A corrected date, a new degree or a new entry on the never-claim
  list must be made in both copies, and a copy that misses it produces a
  resume that disagrees with the other one.
- **Academic fields added to the one document.** The industry bundle then
  carries academic choices, and every reader of it has to know which fields
  belong to which track.

Spec 091's design note settles the storage rule for later: a document is
owned by one track or shared by all, and a track-owned document shadows a
shared one. That rule can only be used once facts and presentation are
separate documents. Today they are not.

Nothing is broken for the operator today. This spec changes the shape of
the stored documents so that spec 099 can add a framing without copying
facts. On the default track every generated output stays the same.

## Scope

- Two profile documents replace `resume_data`: `resume_facts` and
  `resume_framing`.
- `load_bundle` reads both and returns the same `ResumeBundle` it returns
  today.
- The readers of `resume_data` fields outside `load_bundle` read
  `resume_facts`: the skill vocabulary, the never-claim list, and log
  redaction.
- A one-shot command, `harrier profile split-resume`, turns an existing
  `resume_data` document into the pair.
- The committed example and the demo seed become two files.

## Behavior

### The two documents

Both are JSON profile documents, written and read only through
`harrier.profile.store` (ADR-008).

| Kind | Name | Holds |
|---|---|---|
| `resume_facts` | `resume-facts.json` | What is true about the candidate, shared by every track |
| `resume_framing` | `industry.json` | How the industry resume presents those facts |

Every field the bundle has today goes to exactly one of them.

**`resume_facts`:**

- `candidate`: `name`, `location`, `email`, `phone`, `linkedin`,
  `professional_career_start`.
- `roles`: per role `id`, `organization`, `title`, `employment_type`,
  `period`, `counts_towards_professional_experience`, `technologies`.
- `education`, `certifications`.
- `all_skills`, `verified_skills`, `technology_aliases`.
- `forbidden_phrases`.

**`resume_framing`:**

- `candidate`: `primary_identity`, `positioning_technologies`,
  `experience_statement`.
- `profile_summary`.
- `roles`: per role `id`, `competencies`, `bullet_count`,
  `default_bullets`. The `id` names a role in `resume_facts`.
- `bullet_pool`, `evidence_groups`, `default_achievements`.
- `target_signal_weights`, `evaluation_dimensions`.

The bullet pool is framing. A bullet is a worded claim, and a framing words
its own. The truth document (`resume_truth`) and the achievements document
stay where they are: they are the shared check every framing's bullets
pass, through the existing truth gate. The never-claim list is a fact: a
claim untrue on one track is untrue on every track.

Top-level keys that begin with `_` (comments) are allowed in either
document and are not read.

### Loading

`load_bundle(conn)` reads `resume_facts` and `resume_framing`, merges them
by the table above, and parses the result with the existing rules. For the
same content, the `ResumeBundle` it returns is equal to what `parse_bundle`
returns today for the one document that held both halves. Every consumer of
the bundle (tailoring, evaluation, the brief, the offer evaluation, the
artifact listing) is unchanged and produces the same output.

The checks that compare the two halves still run, and each error names the
document its field is in:

- A framing role whose `id` is not a facts role:
  `resume_framing: roles[<i>] names unknown role <id>`.
- A facts role with no framing role keeps today's defaults for an absent
  field: `bullet_count` 2, no competencies, no default bullets.
- A default bullet, an evidence group, a default achievement or a dimension
  evidence reference not in the bullet pool: today's message, prefixed
  `resume_framing:`.
- `experience_statement` stating more years than the facts' career start
  supports (spec 071 O7): today's message, prefixed `resume_framing:`.
- Every existing single-document check (spec 059, spec 062, spec 071) runs
  on the field where it now lives, prefixed with its document.

A key in either document that the table puts in the other one is refused,
naming the key and the document it belongs in. A field cannot be read from
the wrong half by accident, and a field written in both cannot disagree.

### Other readers

- `load_skill_vocabulary` and `load_forbidden_phrases`, used by the cover
  letter and the answers (specs 034, 065), read `resume_facts`.
- Log redaction (`services/api/src/harrier/logredact.py`) reads the
  candidate identity values from `resume_facts`, and from a `resume_data`
  document while one exists. Before the split the operator's values come
  from `resume_data`. After it they come from `resume_facts`. No point in
  between leaves them unredacted.

### Refusal while the old document remains

With a `resume_data` document still in the store and no pair,
`load_bundle`, `load_skill_vocabulary` and `load_forbidden_phrases` raise
`ResumeBundleError`:

`resume content is still one resume_data document; run harrier profile split-resume`

The commands that read the bundle (`tailor`, `cover-letter`, `answers`,
`evaluate`, and `brief set` with confirmed skills) report it and fail with
the exit code each already uses for a `ResumeBundleError` (`tailor`: 1).
This spec changes no exit code. Discovery, screening and the tracker do not read
the bundle and are not affected.

With both `resume_data` and either new document present, the same readers
refuse with
`both resume_data and resume_facts/resume_framing are stored; keep one shape`.
With only one of the pair, they refuse naming the missing one.

### `harrier profile split-resume`

A database command (`COMMAND_CLASSES` class `DATABASE`, spec 074) on the
default track. It reads the `resume_data` document and computes the two new
documents by the table.

- **Default: a dry run.** It prints, for each new document, the field paths
  it would hold (`candidate.name`, `roles[0].title`, ...). It prints no
  field values: the output can reach a run log. It writes nothing and exits
  0.
- **`--write`:** It first checks that the merge of the two computed
  documents parses to a bundle equal to the parse of `resume_data`. Then,
  in one transaction, it writes `resume_facts` and `resume_framing` and
  changes the old row's kind from `resume_data` to `resume_data_presplit`,
  name and content unchanged. It prints the three document names and exits
  0. Nothing reads `resume_data_presplit`. `profile export` still writes it
  byte-identically (spec 004), so the operator keeps the original until
  they remove it.
- The computed documents keep the original's key order within each half
  and are written as JSON indented by two spaces.

## Failure modes

- **No `resume_data` and no pair.** Exit 1:
  `no resume_data document to split`.
- **Already split** (pair present, no `resume_data`). Exit 0:
  `already split: resume_facts and resume_framing are stored`. A second run
  changes nothing.
- **Both shapes present.** Exit 1 with the refusal above. The command does
  not choose between them.
- **`resume_data` is not valid JSON, or does not parse as a bundle.**
  Exit 1 with today's `ResumeBundleError` message. Nothing is written. The
  split never turns an invalid bundle into two invalid documents.
- **A key the table does not place**, at the top level or in a role (other
  than a `_` comment key). Exit 1 naming every such key. Nothing is
  written. A value is never dropped by the split.
- **The equality check fails.** Exit 1:
  `split would change the bundle: <first differing field>`. Nothing is
  written. This is a defect in the split, not in the operator's content.
- **The write fails partway.** The transaction rolls back. The store holds
  `resume_data` as before and no new document.
- **Another kernel owns the database** (ADR-011). The command runs where
  every database command runs: in the container, or delegated to it
  (spec 061). It does not open the database from the host while the
  container runs.
- **`--track <non-default slug>`.** Refused with exit 2, like every
  command not in `NON_DEFAULT_OPERATIONS` (spec 093). Spec 099 owns
  framings on other tracks.

## Acceptance criteria

- [ ] For the committed example, `load_bundle` over the two example
      documents returns a `ResumeBundle` equal to `parse_bundle` over the
      single document the split was computed from
      (planned test_split_bundle_equals_the_one_document_bundle).
- [ ] `tailor` on the demo job produces byte-identical markdown from the
      pair and from the single document it was split from
      (planned test_tailored_markdown_is_unchanged_by_the_split).
- [ ] The cover letter and answers claim checks read the vocabulary and the
      never-claim list from `resume_facts`; an entry added only there is
      enforced (planned test_letters_read_the_never_claim_list_from_facts).
- [ ] Log redaction redacts a name stored only in `resume_facts`, and one
      stored only in `resume_data`
      (planned test_redaction_reads_identity_from_either_shape).
- [ ] `split-resume` with no flag prints field paths, no values, and leaves
      `profile_documents` unchanged
      (planned test_split_resume_dry_run_writes_nothing).
- [ ] `split-resume --write` leaves exactly `resume_facts`,
      `resume_framing` and `resume_data_presplit` (byte-identical to the
      old content), and a second run exits 0 with `already split`
      (planned test_split_resume_writes_the_pair_and_keeps_the_original).
- [ ] A `resume_data` with an unplaced key exits 1 naming it and writes
      nothing (planned test_split_resume_refuses_an_unplaced_key).
- [ ] `load_bundle` refuses: `resume_data` alone (names the command), both
      shapes, one half of the pair, and a key in the wrong document
      (planned test_load_bundle_refuses_each_mixed_shape).
- [ ] A framing role naming an unknown facts role is refused with the
      document prefix (planned test_framing_role_must_name_a_facts_role).
- [ ] `--track <academic slug> profile split-resume` exits 2.
- [ ] `config/resume-facts.example.json` and
      `config/resume-framing.example.json` replace
      `config/resume-content.example.json`; the demo seeds both; the real
      names `config/resume-facts.json` and `config/resume-framing.json` are
      never-in-git in `config/data-classification.json`, `.gitignore` and
      `.dockerignore`.
- [ ] `split-resume` is placed in `COMMAND_CLASSES`; the parser walk test
      passes.
- [ ] `uv run ruff check`, `uv run pyright` and `just check` pass.

## Honest limitations

- **The field table is a judgement.** `all_skills` is an ordering as well
  as a vocabulary, and it sits in facts. A track that wants another skill
  order cannot have one until a later spec moves the ordering. Role
  `technologies` are facts; a framing cannot hide one from a role.
- **The split is mechanical.** It moves fields. It does not reword
  anything, and it does not check that a facts field reads well on an
  academic resume.
- **`resume_data_presplit` stays until the operator removes it.** There is
  no command to delete a profile document, by design here. It is personal
  data in the local database and goes into every backup.
- **`profile import`** from the old repository still writes `resume_data`
  (spec 004 export must reproduce imported files byte-identically). After a
  fresh import the operator runs the split again. That import is a one-time
  migration that has already happened.

## Migration

For the operator, once, after this ships and the container is up:

1. `harrier profile split-resume` and read the field list.
2. `harrier profile split-resume --write`.
3. `harrier tailor` on any job to confirm the resume is unchanged.

Until step 2, the artifact commands fail with the message naming the
command. Discovery
and the tracker keep working.

## Options weighed

- **One document with `facts` and `framing` sections.** Smaller change,
  but spec 091's per-document ownership cannot make a section owned by a
  track. Spec 099 would have to split it anyway.
- **Automatic split on first load.** No operator step, but it rewrites
  personal data without being asked. Declined (Akin, 2026-10-10).
- **The loader reads both shapes for good.** Nothing to migrate, but two
  parse paths stay forever and a stale `resume_data` could shadow the pair.
  Declined (Akin, 2026-10-10).
- **The bullet pool as a shared fact,** with a framing picking ids only.
  The academic framing would then need its research wording in the shared
  pool. Declined (Akin, 2026-10-10).

## Open decisions for Akin

1. **Framing document name.** `industry.json` names the kind. Spec 099
   will add one per academic track; naming it after the track slug instead
   is the alternative.
2. **`experience_statement` in framing.** It states a fact (years), checked
   against facts, but an academic CV words it differently. Placed in
   framing here.
3. **Role `competencies` in framing.** They drive evaluation and planning,
   so they are a presentation choice here. If you see them as facts about
   the role, they move.

## Proof / origin

- The bundle and its loader: `services/api/src/harrier/resume/content.py`
  (`parse_bundle`, `load_bundle`, `_resume_data_fields`,
  `load_skill_vocabulary`, `load_forbidden_phrases`); spec 013.
- The never-claim list: spec 034. The vocabulary: spec 065. Education:
  spec 059. Emitted-string checks: spec 062. The experience statement and
  dashes: spec 071.
- Log redaction's identity read: `services/api/src/harrier/logredact.py`
  (`_candidate_identity_values`).
- The profile store write path: `services/api/src/harrier/profile/store.py`;
  ADR-008. Byte-identical export: spec 004.
- Command classes: `services/api/src/harrier_cli/main.py`
  (`COMMAND_CLASSES`), spec 074.
- The demo seed: `services/api/src/harrier_api/demo.py` (`PROFILE_SEEDS`).
- Per-document track ownership: spec 091, Out of scope, design note.
- The follow-on list this spec opens: spec 097, Out of scope.

## Out of scope

- Track ownership of profile documents, and any framing on a non-default
  track (spec 099).
- An academic framing's fields, such as publications or teaching (spec 099).
- Changes to the truth gate's negation reading (spec 100).
- Academic application documents (spec 101).
- Editing profile documents in the browser (spec 096 Out of scope).
- Placing `split-resume` on spec 096's Settings page. When spec 096 is
  implemented, it goes in the terminal-only table as a one-time migration,
  beside `profile import`.
- Splitting the `candidate` or `application_profile` documents.
- A command to delete a profile document.
