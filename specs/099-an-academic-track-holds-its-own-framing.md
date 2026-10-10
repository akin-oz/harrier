---
spec: 099
title: An academic track holds its own framing over the shared facts
status: accepted
approved: yes
milestone: M9
depends: [004, 034, 074, 090, 091, 093, 097, 098]
---

# Spec 099: An academic track holds its own framing over the shared facts

## Problem

Spec 098 splits the resume content into `resume_facts`, true on every
track, and `resume_framing`, which says how one kind of resume presents
those facts. After 098 there is still one framing, the industry one, and
the store cannot hold another.

The store is the reason. `profile_documents` is unique on `(kind, name)`
(migration 2 in `services/api/src/harrier/tracker/schema.py`), and nothing
in a row says which track it belongs to. So:

- **An academic track has nowhere to keep its framing.** A second
  `resume_framing` row under another name would be read by
  `_document_by_kind` (`services/api/src/harrier/resume/content.py`), which
  takes the first row of a kind by name, on every track. Whichever name
  sorts first would frame both resumes.
- **The operator has no command to write one.** Profile documents are
  written by `profile import` from the old repository, or by hand through
  `put_document` in a Python session. Neither checks the content, so a
  framing with a broken bullet reference is found only when a resume is
  generated, which on an academic track is spec 101's work.

Spec 091's design note planned the store change: a document is owned by
one track or shared by all, with matching unique indexes and upserts.
Spec 093 kept every academic command away from profile documents until
that existed.

The person affected is the operator preparing academic applications. They
need to write research-worded bullets and research dimensions over the
same facts the industry resume uses, and see before spec 101 lands whether
those bullets pass the truth gate.

## Scope

- Migration 10: `profile_documents` gains an owning track.
- Which kinds may be owned: `resume_framing` must be, every other kind must
  not be.
- `load_bundle` reads the shared facts and the scope's own framing.
- `harrier [--track <slug>] profile put resume_framing --file PATH`:
  validated write of the scope's framing.
- `harrier [--track <slug>] profile check`: validation and truth report for
  the scope's resume content, writing nothing.
- `profile list` and `profile export` show and keep the owner.

## Behavior

### Migration 10

Migration 10 rebuilds `profile_documents`, as migration 7 rebuilt
`user_config`, whole or not at all (spec 090):

```sql
CREATE TABLE profile_documents_new (
    id INTEGER PRIMARY KEY,
    track_id INTEGER REFERENCES tracks(id),
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    format TEXT NOT NULL DEFAULT 'text',
    content TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- copy every row with its id, content and updated_at;
-- track_id = 1 (the default track) for kind 'resume_framing', NULL otherwise
DROP TABLE profile_documents;
ALTER TABLE profile_documents_new RENAME TO profile_documents;
CREATE UNIQUE INDEX idx_profile_documents_shared
    ON profile_documents (kind, name) WHERE track_id IS NULL;
CREATE UNIQUE INDEX idx_profile_documents_owned
    ON profile_documents (track_id, kind, name) WHERE track_id IS NOT NULL;
```

`track_id` NULL means shared by every track. SQLite treats NULLs as
distinct in a plain `UNIQUE`, which is why there are two partial indexes
(spec 091's design note). If spec 103 has shipped by the time this is
built, migration 10 also carries its Postgres statements (spec 103 rule 3):
the same two partial unique indexes, which Postgres supports in the same
form. Content is copied byte for byte, so
`profile export` before and after the migration writes the same bytes for
every shared document.

### Which kinds may be owned

The write path (`harrier.profile.store.put_document`) holds the rule, not
a `CHECK`, so a later spec can widen it without a rebuild:

- `resume_framing` is always owned by a track. A shared write is refused.
- Every other kind is always shared. An owned write is refused.

Both refusals raise `ProfileDocumentError` naming the kind. `put_document`
takes an optional track id; its upsert names the conflict target of the
matching partial index.

A track's framing is named after the track's kind: `industry.json` on the
default track (spec 098), `academic.json` on an academic track. Each track
owns at most one framing.

### Reading

Every existing reader of a profile document (`get_document`,
`_document_by_kind`, log redaction, the stories, the outreach defaults, the
brief, the screening candidate) reads shared rows only, as before. None of
them can see an owned row.

`load_bundle(conn, scope)` takes the scope. It reads `resume_facts`
(shared) and the `resume_framing` owned by `scope.track`, and merges them
by spec 098's rules. There is no fallback: a track with no framing of its
own never reads another track's.

- No framing on the scope's track:
  `track <slug> has no resume framing; store one with harrier --track <slug> profile put resume_framing --file PATH`.

Every current caller of `load_bundle` runs on the default track only
(`tailor`, `cover-letter`, `answers`, `evaluate`, `brief set`; spec 093
refuses them elsewhere) and passes its scope. On the default track the
result is the bundle spec 098 returns, so no generated output changes.

`harrier profile split-resume` (spec 098) writes the framing owned by the
default track.

### `profile put resume_framing --file PATH`

A database command with a host path option (`COMMAND_CLASSES`:
`CommandClass(DATABASE, path_options=("file",))`, spec 074). It is in
`NON_DEFAULT_OPERATIONS` and `WRITE_OPERATIONS`, so it runs on an academic
track and is refused on an archived one.

1. Reads the file as UTF-8 JSON.
2. Refuses a key that belongs in `resume_facts` (spec 098), naming it.
3. Merges it with the shared `resume_facts` and parses the result with
   every rule `load_bundle` applies.
4. On success, writes it as the scope track's `resume_framing`, replacing
   any earlier one, and prints
   `stored resume_framing/<kind>.json for track <slug>`. Exit 0.

Any refusal prints every error, writes nothing, and exits 1. The file is
stored as read, byte for byte, so `profile export` gives it back unchanged.

`put` does not run the truth gate. A bullet the truth does not support is
still a valid framing; generating a document with it is what fails
(spec 034). `put` prints `run harrier --track <slug> profile check to see
which bullets the truth supports` after a successful write.

### `profile check`

A database command (`_DB`), in `NON_DEFAULT_OPERATIONS`, read-only.

1. Loads the scope's bundle with `load_bundle(conn, scope)`, and the truth
   sources with `load_truth_sources`.
2. Runs the truth gate (`TruthSources.contains`, as amended by spec 100
   once it lands) on every bullet in the framing's `bullet_pool`.
3. Prints `resume content for track <slug>: valid` or each bundle error,
   then one line per bullet the truth does not support:
   `bullet <id>: not supported by the truth documents`. It prints bullet
   ids, never bullet text or field values.

Exit 0 when the bundle is valid and every bullet is supported, 1
otherwise. On the default track it reports the industry framing the same
way.

### `profile list` and `profile export`

- `profile list` adds the owner to each line: `(shared)` or
  `(track <slug>)`.
- `profile export --to DIR` writes shared documents where it does today,
  `DIR/<kind>/<name>`, and owned documents to
  `DIR/tracks/<slug>/<kind>/<name>`, byte-identical (spec 004).
- `profile import` from the old repository writes shared documents only, as
  today.

### Spec 093's profile-read guarantee

Spec 093 promised that no allowed command reads a profile document on an
academic track, and spec 097 narrowed that to `discover`'s one
configuration row. This spec narrows it again: `profile put` and
`profile check` read `resume_facts`, the truth documents and the track's
own framing, by design. Every other allowed command still reads no profile
document, and
`services/api/tests/test_tracks_cli.py::test_academic_commands_read_no_profile_document`
keeps asserting that for them.

## Failure modes

- **Migration 10 on a store with no `resume_framing`** (spec 098 not yet
  run). Every row becomes shared. `split-resume` later writes the framing
  owned by the default track.
- **Migration 10 fails partway.** Spec 090 rolls the whole migration back;
  the store is at version 9 with the old table.
- **Two `resume_framing` rows before the migration** (written by hand).
  Both are owned by the default track; distinct names keep them unique.
  `load_bundle` on the default track then refuses:
  `track <slug> owns more than one resume framing: <names>`. The operator
  removes one. Nothing is chosen silently.
- **`put` with no `resume_facts` stored.** Exit 1:
  `no resume_facts document; run harrier profile split-resume first`.
- **`put` with a file that is not JSON, not an object, or not UTF-8.**
  Exit 1 naming the problem; nothing written.
- **`put` with a framing whose role ids are not facts roles.** Exit 1 with
  spec 098's `resume_framing: roles[<i>] names unknown role <id>`.
- **`put` on an archived track.** Refused by the write-operation rule,
  exit 2, like every write on an archived track (spec 093).
- **`put` of any other kind** (`profile put resume_facts`). Refused by the
  parser: the only accepted kind is `resume_framing`.
- **`check` on a track with no framing.** Exit 1 with the no-framing
  message from Reading.
- **A shared write of `resume_framing`** through `put_document` from code
  or a Python session. Refused with `ProfileDocumentError`.
- **The facts change after a framing was stored** (a role removed). The
  stored framing is not rewritten. `load_bundle` and `check` on that track
  refuse with the unknown-role error until the operator stores a corrected
  framing.

## Acceptance criteria

- [ ] Migration 10 on a version 9 store keeps every row's id, content and
      `updated_at`, owns `resume_framing` rows by the default track and
      shares the rest; `profile export` output for shared documents is
      byte-identical before and after
      (planned test_migration_10_keeps_every_document_and_owns_the_framing).
- [ ] A failure inside migration 10 leaves the store at version 9 with the
      old table (planned test_migration_10_is_whole_or_nothing).
- [ ] The two partial unique indexes refuse a second shared `(kind, name)`
      and a second owned `(track, kind, name)`, and allow one owned and one
      shared row of the same kind and name
      (planned test_profile_documents_unique_per_owner).
- [ ] `put_document` refuses a shared `resume_framing` and an owned row of
      any other kind (planned test_only_the_framing_is_owned).
- [ ] An academic track's framing is read by `load_bundle` on that track
      and by no reader on any other track; the default track's bundle is
      unchanged by it (planned test_a_track_reads_only_its_own_framing).
- [ ] `load_bundle` on a track with no framing refuses with the message
      naming `profile put`, and never reads another track's framing
      (planned test_no_framing_means_no_fallback).
- [ ] `profile put resume_framing --file` stores a valid framing
      byte-for-byte on the scope's track, and refuses an invalid one,
      a facts key, missing facts and an archived track, writing nothing
      (planned test_profile_put_validates_before_it_writes).
- [ ] `profile check` exits 0 for a valid bundle whose bullets are all
      supported, and 1 listing the ids of unsupported bullets, printing no
      bullet text (planned test_profile_check_reports_bullet_ids_only).
- [ ] `profile list` shows each document's owner, and `profile export`
      writes owned documents under `tracks/<slug>/`
      (planned test_profile_list_and_export_show_the_owner).
- [ ] `test_academic_commands_read_no_profile_document` passes with
      `profile put` and `profile check` named as the exceptions.
- [ ] `tailor` on the demo job produces the same markdown before and after
      migration 10 (planned test_tailored_markdown_is_unchanged_by_ownership).
- [ ] `profile put` and `profile check` are placed in `COMMAND_CLASSES`;
      the parser walk test passes.
- [ ] `uv run ruff check`, `uv run pyright`, `just contract` (no diff) and
      `just check` pass.

## Honest limitations

- **Only the framing can be owned.** The application profile, the
  candidate document and the outreach defaults stay shared. An academic
  application profile is a later spec, and widening the rule is a code
  change in the write path.
- **The facts have no write command.** Editing `resume_facts` is still a
  Python session through `put_document`. A facts change can break a stored
  framing, which `check` then reports.
- **`check` reports; nothing generates.** An academic track still has no
  resume, letter or answers until spec 101. The truth report is the only
  way to see before then whether research-worded bullets will pass.
- **The academic framing has the industry fields.** Research identity,
  research bullets and research dimensions fit them. Publications,
  teaching and grants do not, and need facts that spec 098 does not hold.

## Migration

Migration 10 runs on the next open after this ships (`just container-up`),
whole or not at all. Spec 098's split can run before or after it: before,
the migration gives the framing to the default track; after, the split
writes it there. Then, for each academic track:

1. Write the framing JSON from `config/resume-framing.example.json`, with
   research-worded bullets.
2. `harrier --track <slug> profile put resume_framing --file PATH`.
3. `harrier --track <slug> profile check`, and reword any bullet it names
   or add the supporting line to the truth document.

## Options weighed

- **Framing documents named by track slug, no migration.** Smaller, and
  close to how spec 097 keys `academic_searches` inside one row. But the
  next per-track document needs the same trick or this migration anyway.
  Declined (Akin, 2026-10-10).
- **A track-owned document shadows a shared one of the same kind**, as
  spec 091's note sketched. Not needed while ownership is fixed by kind,
  and a fallback from an academic track to the industry framing is exactly
  the silent mixing this spec exists to stop.
- **A `CHECK` constraint for which kinds may be owned.** Stronger, but
  every widening becomes a table rebuild. Declined in favour of the write
  path.
- **Python-only writes.** No new command, but no validation before the
  write. Declined (Akin, 2026-10-10).
- **Academic-only fields now.** Declined (Akin, 2026-10-10): they need new
  facts, and spec 101 is where a document first uses them.

## Proof / origin

- The table and its migrations: `services/api/src/harrier/tracker/schema.py`
  (migration 2 creates `profile_documents`, migration 7 is the rebuild
  precedent, migration 8 creates `tracks`). Whole-or-nothing: spec 090.
- The store: `services/api/src/harrier/profile/store.py` (`put_document`,
  `get_document`, `list_documents`, `export_to`, `import_from`); ADR-008.
- The bundle reader: `services/api/src/harrier/resume/content.py`
  (`_document_by_kind`, `load_bundle`, `load_truth_sources`); spec 098.
- The bullet truth check: `services/api/src/harrier/resume/markdown.py`
  (`resolve_bullets`); spec 034.
- Track rules: `services/api/src/harrier/tracks.py`
  (`NON_DEFAULT_OPERATIONS`, `WRITE_OPERATIONS`, `DEFAULT_TRACK_ID`);
  specs 091, 093.
- Command classes: `services/api/src/harrier_cli/main.py`
  (`COMMAND_CLASSES`); spec 074.
- Per-document ownership: spec 091, Out of scope, design note.

## Out of scope

- Resumes, cover letters and answers on an academic track (spec 101).
- Changes to the truth gate itself (spec 100).
- Academic-only framing fields and the facts they need (publications,
  teaching, grants).
- Owning any kind other than `resume_framing`, and per-track configuration
  in `user_config`.
- A write command for `resume_facts` or any other document.
- Showing the owner on spec 096's Settings page; when spec 096 is
  implemented its profile list shows the owner `profile list` prints.
- Deleting a profile document, or a framing when its track is archived.
- Tenancy (spec 102): `owner_id` is a separate column in the hosted schema.
