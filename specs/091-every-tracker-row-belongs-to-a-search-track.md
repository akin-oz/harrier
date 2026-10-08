---
spec: 091
title: Every tracker row belongs to a named search track
status: proposed
approved: no
milestone: M9
depends: [023, 041, 061, 079, 090]
---

# Spec 091: Every tracker row belongs to a named search track

## Problem

The tracker holds one search. Every row in `jobs` is a posting from one
job hunt, screened by one candidate configuration, ranked by one scoring
policy, and written to one queue. Nothing in the schema says so, because
nothing ever needed to: `data/tracker.db` has tables for jobs, contacts,
profile documents, user configuration, job runs, job events and the schema
version, and not one of them carries a column saying which search a row
belongs to.

A second kind of search breaks that silently. An academic search track (for
example university or research vacancies) has different sources, different
fit rules, no remote-only gate, deadlines instead of a follow-up cadence,
and its own application documents. Put its rows in today's tracker and
every reader mixes them in: the queue ranks them by an industry score they
never earned, `harrier reevaluate` scores them against the industry
candidate configuration, the digest counts them as prospects, the learned
model (spec 077) trains on their decisions, and the profile readers pick
whichever document sorts first
(`services/api/src/harrier/apply/profile.py:43-48` and
`services/api/src/harrier/resume/content.py:674-679` take the first row by
name; `services/api/src/harrier/logredact.py:48-70` takes an unordered
first row; `services/api/src/harrier/screening/config.py:33` reads one exact
name). Put them in a second database and every command, backup, schedule and
page has to learn which file it means.

Two further things are decided here rather than later, because the shape
of the first decision depends on them.

**Tracks are not tenants.** A track is a second kind of search by the same
person; a tenant is a second person. The product is meant to be deployed
for other people one day, so tenancy is a real direction and not a thought
experiment, and ADR-009 already hands tenant isolation to "a future
multi-tenant ADR" (`docs/adr/ADR-009-user-configuration-and-tenancy.md:60-62`).
This spec carries that ADR, ADR-012, so that tracks are built on a decided
answer to "where will tenants live" and not on a guess that a later ADR has
to unpick.

**Spec 041's bar.** Spec 041 removed the `scope` column from `user_config`
because it only ever held `default`, and ADR-009's amendment says why: "A
column that is always `default` blocks nothing and proves nothing." Any
column this spec adds has to clear that bar. `track_id` does, by
construction: spec 093 creates a second track, so the column holds a second
value as soon as the milestone's next spec lands, and every reader is
changed to use it (spec 092). No tenant column is added, because no second
tenant exists to give it a value.

## ADR-012: Search tracks are rows; tenants are data directories

This section is the ADR's full text. The implementing branch writes it to
`docs/adr/ADR-012-search-tracks-and-tenant-isolation.md` with the status
decided under Open decisions.

### Context

ADR-003 made SQLite the tracker's source of truth: one file, one write
path. ADR-008 put every personal document in that file. ADR-009 made user
configuration data in the same file and promised that nothing would block
tenancy, then (amended by spec 041) withdrew the one column that pretended
to deliver it. ADR-011 made one kernel the owner of the file at a time.

Two directions now need a decision that those four ADRs left open:

1. A second kind of search for the same person, with its own rules,
   sources and documents, inside the same install.
2. A second person. Locally, that is a second install on the same machine
   or a second data directory. Hosted, it is a service with accounts, which
   is the deployment the product is meant to reach.

The question is which boundary each of these crosses, because the answer
decides where a predicate lives. A boundary crossed by a row needs a column
and a `WHERE` clause in every reader, forever. A boundary crossed by a store
needs none of that: the reader cannot see what it was not opened on.

### Options weighed

**(a) Tracks plus a `tenant_id` column on every personal table.** One
file holds everyone. Every reader filters by two keys. A forgotten
predicate leaks one person's rows to another, and SQLite has no row-level
policy to catch it, so the discipline is in every query. The column holds
one value until a second person exists, which is the shape spec 041 removed.
Backup, restore, `doctor`, export and redaction all have to learn tenancy
on the day the column lands. Rejected.

**(b) One SQLite file per tenant, tracks inside it as rows.** Tenant
isolation is the file system: a process opened on one directory cannot read
another. The domain's SQL names a track and never a tenant. The current
install is already the default tenant, in place, with nothing to migrate.
Backup and restore of one person are a directory copy. Spec 041's bar is
met: `track_id` takes its second value in spec 093, and no tenant column
exists until a tenant does. The costs: tenant selection becomes a question
at every process boundary (a flag or environment value for the CLI, the
session for the API, the plist for launchd); a cross-tenant operation (none
exists) is N opens; and where the other directories live matters, because
`just backup` copies `data/` as a tree and would raw-copy a nested live WAL.
Chosen for the local product.

**(c) A shared schema with a users table and a scoped repository.** Every
store function takes a user, and the store applies it. This is (a) with the
discipline moved into one module, and it is the shape a hosted service on a
database with row-level policies ends up with. On SQLite it has (a)'s costs
without the database's help. Not chosen for the local product; it is the
shape ADR-013 starts from for the hosted one.

### Decision

1. **A search track is a row.** `tracks` is a table; `jobs.track_id` names
   one; every reader of tracker rows reads one track (spec 092). This holds
   in every deployment, local or hosted, because a track is something one
   person has several of.
2. **A tenant is a store boundary, never a predicate the domain writes.**
   Locally, the boundary is a data directory with its own `tracker.db`; the
   checkout's `data/` is the default tenant, in place. In a hosted
   deployment the boundary is the database's own row-level policy, keyed by
   the authenticated owner, applied by the database from the session and
   not by the domain's SQL. ADR-013 decides that host, its authentication,
   and its schema; it inherits from this ADR that domain code filters by
   track and never by tenant.
3. **Nothing in the local schema names a tenant.** No `tenant_id`, no
   `owner`, no users table, until ADR-013 adds them to the hosted schema,
   which spec 041 already records as a migration and an accepted cost.
4. **"One tracker, one write path" holds per store.** Each tenant's store
   has exactly one tracker and is written by exactly one module,
   `harrier.tracker`. The invariant's words do not change; ADR-003 gains a
   note saying which file "the tracker" is: the one in the data directory
   the process was opened on.
5. **Schema constructs are chosen with a port in mind.** The DDL here is
   SQLite's. A table, a CHECK, a trigger, an index and a partial unique
   index each have a direct equivalent in the database ADR-013 is likely
   to choose; the one SQLite-only construct, `GLOB` in the slug CHECK, has
   a regular-expression CHECK as its equivalent, and the slug rule is also
   enforced in `harrier.tracks` so the CHECK is a backstop and not the only
   copy of the rule.

### Consequences

- Migration 8 (below) adds `tracks` and `jobs.track_id`, and seeds the one
  track every existing row belongs to.
- Spec 092 makes every reader take a scope. Spec 093 creates the second
  track and gives the CLI a way to name one. Both are the proof that the
  column is not speculative.
- Where other tenant directories live when a second local tenant exists
  is deferred to that day, with two constraints already fixed. Any location
  inside the checkout is classified never-in-git in
  `config/data-classification.json` and matched by `.gitignore` before the
  directory can exist, because a second person's `tracker.db` beside
  `data/` would otherwise be staged by `git add -A` (today only `/data/`
  and its descendants are ignored). And the backup and restore commands
  learn to skip or include those directories before any can exist,
  because today's tree copy would raw-copy a nested live WAL.
- The hosted deployment is ADR-013's: authentication, tenant resolution at
  every boundary, the row-level policy, migration tooling for its database,
  the amendment to the local-first invariant, and the addition to the
  closed list of cloud dependencies. None of that is started here.
- ADR-009's status line becomes "accepted; extended by ADR-012
  (isolation)". It is not superseded: its decision that configuration is
  data read through accessors stands, and its point 3 explicitly handed
  isolation on. ADR-003 gains the per-store note above.

### Honest limitations

- No second tenant exists, so the local half of the decision is proven only
  by what it refuses to add (a tenant column) and by the direction it
  leaves open, not by two directories side by side.
- The hosted half is a direction with an ADR to write. Naming row-level
  policies here is a constraint on ADR-013, not a design of it.
- Isolation by directory is as strong as the process's choice of
  directory. A process pointed at the wrong `HARRIER_DATA_DIR` reads the
  wrong person. That is ADR-011's class of problem (the rule covers
  harrier's own code only) and is not made worse here.
- One SQLite file per person does not serve many people from one service.
  That is why the hosted path is a different boundary and a different ADR,
  not a bigger version of this one.
- Cross-tenant reporting is not a feature and this ADR makes it harder on
  purpose.

## Scope

Every file the implementing branch touches, in three groups.

**Code, configuration and tests**

- `services/api/src/harrier/tracker/schema.py`: migration 8 (a guarded
  path; approved under this spec).
- `services/api/src/harrier/db.py`: only if migration 8 needs something
  from the runner spec 090 did not give it. Expected: no change.
- `services/api/src/harrier/tracks.py` (new): `Track`, `TrackKind`,
  `TRACK_KINDS`, `Scope`, the slug rule, `resolve_scope`, `default_scope`
  and `list_tracks`.
- `services/api/src/harrier/tracker/store.py`: `add_job` takes a `Scope`
  and stamps `track_id` from it; `DuplicateJobError` messages name the
  existing row's track.
- The four callers of `add_job`, each passing the default scope resolved
  once at its entry point: `services/api/src/harrier/discovery.py`,
  `services/api/src/harrier/capture.py`,
  `services/api/src/harrier/tracker/migrate_legacy.py`,
  `services/api/src/harrier/tracker/actions.py`.
- `services/api/src/harrier_cli/main.py`: the `tracks list` command and
  its `COMMAND_CLASSES` entry.
- `services/api/pyproject.toml`: `harrier.tracks` joins the
  `forbidden_modules` of the "sources are ingestion only" contract (lines
  96 to 110 at cd5665d).
- `services/api/tests/test_tracks.py` (new) and the extension of
  `services/api/tests/test_scoring.py::test_a_migrated_database_matches_a_fresh_one`
  to migration 8.

**Docs and ADRs**

- `docs/adr/ADR-012-search-tracks-and-tenant-isolation.md` (new): the text
  above.
- `docs/adr/ADR-009-user-configuration-and-tenancy.md`: the status line
  only, "accepted; extended by ADR-012 (isolation)".
- `docs/adr/ADR-003-tracker-store.md`: one note under Decision, that "the
  tracker" is the file in the data directory the process was opened on,
  one per tenant (ADR-012).
- `docs/architecture.md`: a `harrier.tracks` row in the module table, and
  a line under Honest limitations that tracks partition one person's
  tracker and do not separate people.
- `README.md`, the "Single user, single machine" limitation (lines 261 to
  268 at cd5665d): it still says there is no tenant resolution, and now
  says search tracks are not tenancy and points at ADR-012.

**Spec amendments on the implementing branch**

- `specs/023-user-configuration-in-db.md`, Proof / origin: its honest
  limitation that "none of this is multi-tenancy" gains a sentence that
  tracks (spec 091) are not tenancy either, and that configuration stays
  one row per kind until per-track configuration lands.

Not touched: the API, the contract, `apps/web`, the schedule, every reader
of tracker rows (spec 092), and the classification table (specs, docs and
source are public; the database is already never-in-git).

## Behavior

### Migration 8

Applied by spec 090's runner, so it lands whole or not at all. Statements,
in order:

```sql
CREATE TABLE tracks (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE CHECK (
        length(slug) BETWEEN 1 AND 32
        AND slug GLOB '[a-z]*'
        AND slug NOT GLOB '*[^a-z0-9-]*'
    ),
    kind TEXT NOT NULL CHECK (kind IN ('industry', 'academic')),
    label TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    archived_at TEXT
);
INSERT INTO tracks (id, slug, kind, label) VALUES (1, 'job', 'industry', 'Job search');
ALTER TABLE jobs ADD COLUMN track_id INTEGER NOT NULL DEFAULT 1;
CREATE TRIGGER jobs_track_must_exist_on_insert BEFORE INSERT ON jobs
BEGIN
    SELECT RAISE(ABORT, 'unknown track')
    WHERE NOT EXISTS (SELECT 1 FROM tracks WHERE id = NEW.track_id);
END;
CREATE TRIGGER jobs_track_must_exist_on_update BEFORE UPDATE OF track_id ON jobs
BEGIN
    SELECT RAISE(ABORT, 'unknown track')
    WHERE NOT EXISTS (SELECT 1 FROM tracks WHERE id = NEW.track_id);
END;
CREATE TRIGGER tracks_are_never_deleted BEFORE DELETE ON tracks
BEGIN SELECT RAISE(ABORT, 'tracks are archived, never deleted'); END;
CREATE INDEX idx_jobs_track_status ON jobs(track_id, status);
```

- The `kind` CHECK list is derived in code from `TRACK_KINDS`, the way
  `_STATUS_LIST` is derived from `STATUSES`. Adding a kind is a migration
  that widens the CHECK plus the policy that kind needs; it is never a
  runtime insert.
- `track_id` is added with `ALTER TABLE ... ADD COLUMN`, so `jobs` is never
  rebuilt. SQLite refuses a `REFERENCES` clause on an added column whose
  default is not NULL, which is why the referential rule is two triggers
  rather than a foreign key. `job_events` is not touched: an event's track
  is its job's.
- Every existing row lands in track 1 through the column default. The
  default track's slug is `job` and its kind `industry`; its label is
  display text and runtime data like every other label.
- The delete trigger keeps a `jobs` row from ever naming a track that is
  gone. Archiving (spec 093) is the only lifecycle verb a track has.
- `contacts` stay person-level. A contact belongs to the person, not to a
  search.

### `harrier.tracks`

- `TrackKind` is `Literal["industry", "academic"]`; `TRACK_KINDS` is the
  tuple the CHECK derives from.
- `Track` is a frozen dataclass: `id`, `slug`, `kind`, `label`,
  `created_at`, `archived_at` (empty when live).
- `Scope` is a frozen dataclass holding the `Track` a command or request
  works in. It is a value passed down, never a module global or a context
  variable, so a test can build two and prove they do not mix. It exists as
  its own type so that signatures do not change again when per-track
  configuration lands and a scope also says which configuration rows apply.
- The slug rule, `validate_slug`: one to thirty-two characters, a lowercase
  letter first, then lowercase letters, digits and hyphens. The same rule
  the CHECK enforces, so a refusal happens in code with a message and the
  database remains the backstop.
- `default_scope(conn)` resolves track 1. `resolve_scope(conn, slug)`
  resolves a track by slug and raises `UnknownTrackError` naming the slug
  when there is none. `list_tracks(conn)` returns every track, archived
  ones included, in id order.
- No function in this module inserts, updates or archives a track. The
  one track is seeded by migration 8. Creating a second is spec 093.

### The write path

`add_job(conn, fields, *, scope)` writes `track_id = scope.track.id`. A
`track_id` key in `fields` is refused with `TrackerError`: the track is the
scope's to say, not the caller's. The `created` event (spec 079) is
unchanged; it carries no track because its job does.

The four callers pass `default_scope(conn)`, resolved once where the
connection is opened. Their behavior does not change: every row they add
lands in track 1, which is where every row landed before.

### `harrier tracks list`

A read-only `database` class command (spec 074). It prints one line per
track: id, slug, kind, label, and `archived` when `archived_at` is set. It
is the only new command here. Its `COMMAND_CLASSES` key is `tracks list`,
and `services/api/tests/test_delegation.py::test_every_subcommand_has_exactly_one_class`
fails until the key exists.

### Sources stay ingestion only

`harrier.tracks` joins the forbidden modules of the "sources are ingestion
only" contract. A source module returns normalized jobs and knows nothing
about which track they will land in; the shared screening path decides
that, once (product invariant "Ingestion only").

## Failure modes

- **Migration 8 on a database with rows.** Every row gets `track_id = 1`.
  Under spec 090's runner, a failure anywhere in the statement list leaves
  the database at version seven with no `tracks` table and no column.
- **A raw INSERT or UPDATE naming a track that does not exist.** Refused by
  the trigger with "unknown track". The write path never produces one,
  because the scope was resolved from the `tracks` table.
- **A DELETE on `tracks`.** Refused by the trigger.
- **A slug outside the rule**, in code or in the database. Refused by
  `validate_slug` with a message naming the rule, and by the CHECK without
  one.
- **`add_job` called with no scope.** A `TypeError` at the call, because
  the parameter is required with no default. A caller cannot forget.
- **`fields` carrying `track_id`.** Refused with `TrackerError`.
- **A fresh database.** Migration 8 runs after migrations one to seven and
  seeds the default track; `test_a_migrated_database_matches_a_fresh_one`
  holds the two paths together.

Must not introduce: a second writer of `jobs` or `job_events`; a rebuild of
`jobs`; any write to `job_events`; a tenant column or a users table; a
`scope=None` default on any signature; a module-level or process-global
"current track"; a source module that imports `harrier.tracks`; a change to
what any reader returns (that is spec 092's, and until it lands every
reader still returns every row, which is correct while every row is in
track 1).

## Acceptance criteria

Tests in `services/api/tests/test_tracks.py` unless named otherwise. Every
database is built under `tmp_path` with synthetic rows.

- [ ] A database at version seven holding synthetic rows migrates to
      version eight, every row reads `track_id = 1`, and `tracks` holds
      exactly the seeded row with slug `job` and kind `industry`
      (planned test_migration_8_keeps_every_row_in_the_default_track)
- [ ] The `job_events` rows are value-for-value identical before and after
      migration 8, and the append-only triggers still fire; the second half
      is `services/api/tests/test_job_events.py::test_job_events_is_append_only`
      (planned test_migration_8_never_touches_job_events)
- [ ] A raw INSERT into `jobs` naming an unknown track, and a raw UPDATE of
      `track_id` to one, are refused by the database; an INSERT naming
      track 1 passes; a DELETE on `tracks` is refused
      (planned test_an_unknown_track_is_refused_by_the_database)
- [ ] `add_job` writes the scope's track and refuses a `track_id` in
      `fields` (planned test_the_write_path_stamps_the_scopes_track)
- [ ] Each shape outside the slug rule (empty, too long, a leading digit or
      hyphen, an upper-case letter, a space, a dot) is refused by
      `validate_slug` and by the CHECK, and `job` passes both
      (planned test_a_slug_outside_the_rule_is_refused)
- [ ] `resolve_scope` with an unknown slug raises `UnknownTrackError`
      naming the slug, and `default_scope` is track 1
      (planned test_resolve_scope_names_an_unknown_slug)
- [ ] `harrier tracks list` prints the default track and is a `database`
      class command
      (planned test_tracks_list_prints_the_default_track;
      `services/api/tests/test_delegation.py::test_every_subcommand_has_exactly_one_class`)
- [ ] `services/api/tests/test_scoring.py::test_a_migrated_database_matches_a_fresh_one`
      covers migration 8
- [ ] A source module that imports `harrier.tracks` fails `uv run
      lint-imports`, checked by adding such an import on a scratch branch
      and recording the failure in the pull request
- [ ] The full suite passes with no reader changed: every reader still
      returns every row, and every row is in track 1
- [ ] ADR-012 exists with the text above; ADR-009 carries the extended
      status line; ADR-003 carries the per-store note; `docs/architecture.md`
      and the README limitation are updated; spec 023's Proof / origin is
      amended
- [ ] No real tracker row, count, institution or person appears in a
      fixture, this spec, the ADR or a commit message (ADR-008)
- [ ] All gates green on the pull request

## Honest limitations

- **One track is not a partition.** Until spec 093, every row is in track
  1 and the column proves only that the write path stamps it. The claim
  that readers isolate tracks is spec 092's to prove, and it is proven with
  a second track a test inserts directly.
- **The slug CHECK uses `GLOB`**, which is SQLite's. The rule is also in
  code; if ADR-013 chooses Postgres, its form is a regular-expression CHECK,
  ported rather than redesigned.
- **Two kinds, fixed in code.** `industry` and `academic` are the kinds
  that have a policy. A kind is not a label the operator can invent at
  runtime, because screening, scoring, the queue and the status labels are
  code that depends on it. Adding one is a migration plus that code.
- **Archiving has no behavior here.** `archived_at` exists so that spec
  093's `tracks archive` is a column write and not a migration. Nothing
  reads it in this spec.
- **The profile readers listed in Problem are not fixed here.** They mix
  profiles only once two profiles exist, and per-track documents are a
  later spec. Until then no command allowed on a second track reads any of
  them (spec 093).

## Migration

For the operator: `just container-up` after spec 090 has landed, then any
command. Migration 8 runs on the next open, whole or not at all. Nothing
to configure. `harrier tracks list` shows the one track.

## Options weighed

The tenancy options are in the ADR section. Implementation choices:

- **Foreign key versus triggers** for `jobs.track_id`. A foreign key needs
  the column added with a NULL default and then backfilled, or a table
  rebuild. Both are worse than two triggers that say the same thing.
- **Nullable `track_id`** meaning "the default track". Rejected: a row with
  no track is the ambiguity this spec removes, and NULL would make every
  reader write `COALESCE`.
- **Index on `(track_id, status)`** rather than on `track_id` alone. The
  queue and the counts filter by both; the existing `idx_jobs_status`
  stays for the readers that filter by status only until spec 092 moves
  them.
- **A `Scope` type versus passing `Track`.** A scope will carry more than
  the track once configuration is per track; naming the type now keeps the
  next change from touching every signature a second time.

## Open decisions for Akin

1. **ADR-012's status on landing.** Recommendation: accepted, by the
   approval of this spec; it is the decision this spec implements. ADR-011
   landed proposed and is still proposed; that is a worse precedent than a
   better one.
2. **ADR-011's own acceptance.** It belongs to spec 061's record, not
   here. Recommendation: accept it in a specs-and-docs-only change under
   spec 061, since specs 074 and 075 have shipped against it.
3. **Where a second local tenant's directory lives.** Recommendation:
   outside the checkout, named by `HARRIER_DATA_DIR`, which `data_dir()`
   already honours; nothing under the repository then needs a new
   classification entry, and `just backup` and the never-in-git patterns
   are unchanged for the default tenant. The in-checkout alternative,
   `data/tenants/<name>/`, is already ignored but sits inside the tree
   `just backup` copies. Deferred until a second tenant exists; recorded
   here so the backup code is not written against the other shape in the
   meantime.
4. **The hosted store.** A managed Postgres with row-level policies is
   the candidate, and ADR-013 should be written before any hosted spec,
   amending the local-first invariant and the closed list of cloud
   dependencies explicitly rather than by implication. Recommendation:
   write ADR-013 only when the first hosted spec is proposed, so it is
   decided against a concrete need.
5. **Kinds as code constants** with the CHECK derived from them, versus a
   `track_kinds` table. Recommendation: code constants, because every kind
   needs code (its policy, its labels, its queue order); a table would hold
   names that nothing can act on.

## Proof / origin

- The schema with no track column: `services/api/src/harrier/tracker/schema.py`
  (migrations one to seven at cd5665d); the next free version is eight,
  checked on every local and remote branch before this spec was written.
- The unique indexes dedupe relies on: `schema.py:133-137`;
  `find_duplicate` in `services/api/src/harrier/tracker/store.py:77-96`; the
  index build at `services/api/src/harrier/discovery.py:176`.
- The readers that would mix profiles: `apply/profile.py:43-48`,
  `resume/content.py:674-679`, `logredact.py:48-70`, `screening/config.py:33`.
- The write path: ADR-003; `add_job` at `store.py:99`; its four callers at
  `discovery.py`, `capture.py:80` onward, `tracker/migrate_legacy.py:164`,
  `tracker/actions.py` (`add_manually`).
- The tenancy direction and the scope column's removal: ADR-009 (points 3
  and its amendment), spec 041 ("What the implementation decided"), spec
  023 (Scope and Proof / origin).
- One kernel and the runner: ADR-011, spec 061, spec 090.
- Events derive their track from their job: spec 079 (the `job_events`
  schema, `job_id` references `jobs`).
- Command classes: spec 074, `COMMAND_CLASSES` at
  `services/api/src/harrier_cli/main.py:1696-1772`.
- The import contract: `services/api/pyproject.toml:96-110`.

## Out of scope

One line each. Each is future work with its own spec.

- Every reader of tracker rows taking a scope (spec 092).
- Creating, naming, archiving or selecting a track from the CLI (spec
  093); the API, the contract and the web track switcher (later).
- Renaming a track or changing its kind.
- Per-track profile documents and per-track configuration. Design note
  for that spec: SQLite treats NULLs as distinct in a UNIQUE constraint, so
  a shared row needs a partial unique index or a non-NULL sentinel for "all
  tracks", and the upserts at `services/api/src/harrier/profile/store.py:31-37`
  and `services/api/src/harrier/userconfig/store.py:153-155` need matching
  conflict targets. Sharing is per document, not fixed by kind: a document
  is owned by one track or shared by all, a track-owned document shadows a
  shared one of the same kind and name, and a user who wants one profile
  across several tracks keeps it shared. Log redaction then loads the
  identity values of every track's documents, not the first row it finds
  (`services/api/src/harrier/logredact.py:48-70`), or a value only the
  second track's document carries reaches the log unredacted.
- Screening and scoring policy chosen by track kind (never by source).
- Sources and importers for an academic track; scheduled discovery across
  tracks.
- Academic opportunity modelling: opportunity kinds, deadlines beyond the
  one column spec 093 adds, references, per-opportunity requirements and
  contact policy, fit shown as components.
- Academic artifacts through the truth and claims gates.
- Tenant plumbing: backup, restore, `doctor` and migrate over every tenant
  store before any second tenant can exist; redaction across tenants;
  tenant selection at every boundary; authentication and hosting
  (ADR-013).
- Refusing a database newer than the code (spec 090, Open decisions).
