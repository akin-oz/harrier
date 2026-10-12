---
spec: 112
title: The tracker domain runs on either store
status: accepted
approved: yes
milestone: M10
depends: [076, 079, 090, 092, 103, 105, 115]
---

# Spec 112: The tracker domain runs on either store

## Problem

Spec 103 creates a Postgres store with the tracker's schema, and nothing
uses it. Every domain read and write is written for `sqlite3`. Line numbers
below are from the spec 103 branch on 2026-10-10, except the
`harrier_api/app.py` ones, refreshed to `main` at `524dd8a` on 2026-10-11,
the `harrier_cli/main.py` ones, refreshed to `main` at `a7b9998`, the
`schema.py` ones, refreshed to `main` at
`5c3d083` after migration 16, and the `pgstore.py` and `connection.py`
ones, refreshed by PR #245; counts say how they were taken.

**The connection type.** `harrier.db.connect` returns a
`sqlite3.Connection` with `sqlite3.Row` rows (`services/api/src/harrier/db.py:222`,
`:236-237`). 45 files under `services/api/src` name `sqlite3.Connection`,
158 times (`grep -rl` and `grep -ro` for `sqlite3\.Connection`). 31 of those
45 contain no `.execute(` call: their only tie to SQLite is the annotation.
Under `services/api/tests`, 33 files name it, 463 times.

**Where the SQL is.** Eight domain modules execute SQL (lines containing
`execute(`, counted per file): `tracker/store.py` (22), `tracks.py` (5),
`userconfig/store.py` (4), `profile/store.py` (4), `runoutcome.py` (3),
`logredact.py` (2), `apply/profile.py` (2), `resume/content.py` (1). The
stub named a ninth, `mail/watch.py`; it has no SQL of its own, and its
`tracker_rows` calls `list_jobs` (`mail/watch.py:580-582`). Four more
modules execute SQL and are local by nature: `backup.py:106-133`,
`doctor.py:199-204`, `cutover.py:113` and `:298-300`, and
`tracker/migrate_legacy.py:98-171`. Two more sit at the edges:
`harrier_api/app.py:270` (the health count) and `harrier_cli/main.py:1600-1615`
(`harrier store status` reading a SQLite file read-only).

**What does not run on Postgres as written:**

- `?` placeholders in every statement. psycopg 3 takes `%s`.
- `with conn:` as a transaction: 14 sites in 5 modules (`tracker/store.py`
  7, `tracks.py` 2, `userconfig/store.py` 2, `profile/store.py` 2,
  `tracker/migrate_legacy.py` 1). (Corrected 2026-10-11, post-merge review
  of PR #241: this line said 13 sites and `profile/store.py` 1, which
  missed the second site in `profile/store.py`. `git grep -c "with conn:"`
  at the commit before PR #241, `96ac216^1`, counts 2 there.) On a `sqlite3.Connection` it commits or
  rolls back and leaves the connection open (spec 076). On a psycopg
  connection the same statement closes the connection on exit. Also
  `conn.commit()` at `runoutcome.py:152`, and `in_transaction`, `commit`
  and `rollback` in `_write_lock` (`tracker/store.py:297-306`).
- `datetime('now')`: 7 sites (`tracker/store.py:424`, `:485`;
  `tracks.py:338`; `userconfig/store.py:170`, `:173`; `profile/store.py:32`,
  `:36`).
- `COLLATE NOCASE` in the company and title dedupe
  (`tracker/store.py:105`, `:110`). Postgres has no `NOCASE` collation.
  SQLite's `NOCASE` folds the 26 ASCII letters and nothing else. Postgres
  `lower()` follows the database locale, so `ÉCOLE EXEMPLE` and
  `école exemple` are equal under `lower()` and different under `NOCASE`.
- Text ordering. `ORDER BY` over text at `userconfig/store.py:208`,
  `profile/store.py:53`, `apply/profile.py:45` and `:56`, and
  `resume/content.py:676`. SQLite orders text by bytes. Postgres orders by
  the database's collation. Byte order puts `_` before every letter; many
  locale collations skip punctuation on the first pass, so `a_z` and `ab`
  can sort either way. Two of these take `LIMIT 1`, so a different document
  can be chosen.
- `cursor.lastrowid` (`tracker/store.py:187`, `:699`).
- `sqlite3.IntegrityError` caught at `tracker/store.py:182` (mapped to
  `DuplicateJobError`) and `tracks.py:324` (mapped to
  `DuplicateTrackError`); `sqlite3.Error` caught at `logredact.py:93`,
  `:194` and `logsetup.py:140`. psycopg raises its own classes. A trigger
  refusal on Postgres comes from `harrier_refuse()`
  (`tracker/schema.py:563-567`) as `psycopg.errors.RaiseException`, which
  is not an integrity error there. An unknown track is a trigger message in
  SQLite (`tracker/schema.py:378-389`) and a foreign key violation in
  Postgres (`tracker/schema.py:600`).
- The write lock. `_write_lock` takes `BEGIN IMMEDIATE`
  (`tracker/store.py:285-306`) for `set_status` (`:355`), `update_fields`
  (`:471`) and `backfill_events` (`:661`), so the row a write depends on is
  read under the lock (spec 079, amendment of the review of the fixes).
  SQLite's write lock covers the whole file. Postgres has no
  `BEGIN IMMEDIATE`, and a store-wide lock there would make every owner's
  write wait for every other owner's.
- Waiting. SQLite waits 5000 ms for a busy writer (`db.py:32`, `:251`) and
  then raises `sqlite3.OperationalError`, which tests catch
  (`test_job_events.py:614`, `test_concurrent_writers.py:136`). Postgres
  waits for a lock without limit unless `lock_timeout` is set, and
  `lock_timeout = 0` means no limit, not no wait.

**The gate.** Until this spec, every CLI command except `harrier store`
refuses a Postgres URL (`harrier_cli/main.py:3091-3094`) and so does the
API (`harrier_api/app.py:1370-1371`), both with `POSTGRES_NOT_YET`
(`connection.py:42-44`; in `pgstore.py` until PR #241 moved it on
2026-10-11), which says "until spec 112".

## Scope

**Narrowed (2026-10-11).** This spec now covers the seam only: items 1,
2, 5 and 7, the placeholder part of item 4, and item 3 and item 13, which
PR 1 built. The rest of item 4, and items 6, 8, 10 and 12, moved to spec
126; item 9 to spec 127; items 11, 14 and 15 to spec 128. The Amendment
(2026-10-11): split, owner and nesting, at the end of this file, says
what this spec delivers now and where each item went.

1. **One connection type.** A new module,
   `services/api/src/harrier/connection.py`, defines `Connection`, `Row`,
   `transaction()` and the store errors (Behavior). Every domain function
   that takes a connection takes this type.
2. **Two openers return it.** `harrier.db.connect` returns a `Connection`
   over SQLite, with the ownership checks, WAL, busy timeout and migration
   it has today. `harrier.pgstore.open_postgres_store` returns a
   `Connection` over Postgres, with spec 103's version check. A new
   `harrier.connection.open_store()` opens whichever store
   `HARRIER_DATABASE_URL` names.
3. **The annotations.** Every `sqlite3.Connection` annotation in
   `services/api/src` and `services/api/tests` that a domain connection
   flows through becomes `Connection`. This is a mechanical change and the
   pull request says so. Raw `sqlite3` stays only where SQLite itself is
   the subject: `db.py` (opening, WAL, migrations), `backup.py`,
   `doctor.py`, the read-only version peek in `harrier_cli/main.py`, and
   tests of migration history and SQLite pragmas.
4. **Portable SQL in the eight modules.** Placeholders, timestamps, case
   folding, text ordering, new ids, upserts and transactions, as Behavior
   states. No statement in the eight modules is written twice.
5. **One set of store errors.** Driver errors raised through a
   `Connection` reach callers as `StoreDatabaseError` or one of its
   subclasses, the same class for the same refusal on both stores. The
   five catch sites listed under Problem catch these.
6. **The write lock per store.** SQLite keeps `BEGIN IMMEDIATE`. Postgres
   locks the job rows the write depends on (Behavior).
7. **A bounded wait on both.** Postgres transactions opened through
   `Connection` set `lock_timeout` to the connection's busy timeout, which
   defaults to `BUSY_TIMEOUT_MS`.
8. **Local-only functions refuse a Postgres connection.**
   `cutover.preflight`, `cutover.run_cutover`, `cutover.tracker_check`,
   `cutover.verify` and `tracker.migrate_legacy.migrate` raise
   `StoreLocalOnlyError` when given one. `backup.py` and `doctor.py` take
   paths, not connections, and are refused by the CLI gate.
9. **The gate's refusals tell the truth.** The CLI still runs only
   `harrier store` on a Postgres URL (Open decision 8). Its refusal names
   why, in two classes: local-only commands, and everything else. The API
   still refuses to start, and now names spec 104.
10. **The tests run on both stores.** A store fixture parametrized over
    SQLite and Postgres, built on `services/api/tests/pg_support.py`, runs
    the domain tests of seven files on both (Behavior). Postgres
    parameters skip locally without a test server and fail in CI without
    one, as spec 103 established.
11. **psycopg has two importers.** An import-linter contract in
    `services/api/pyproject.toml` forbids `psycopg` everywhere except
    `harrier.pgstore` and `harrier.connection`. The post-merge review of
    PRs #194 and #207 (data integrity 6, 2026-10-10) asked for this
    contract independently. Today `harrier/pgstore.py` is the only module
    under `services/api/src` that imports `psycopg` (checked with `grep`
    for `import psycopg` and `from psycopg`), and nothing enforces it.
    `just check` runs `lint-imports` (`justfile:75`).
12. **Docs.** `docs/architecture.md`'s hosted section says the domain runs
    on either store, names `harrier/connection.py`, and stops saying "until
    spec 112". `specs/103-the-tracker-store-speaks-postgres.md` is not
    edited.
13. **Store-neutral names leave `pgstore.py`.** `StoreError` and its
    subclasses, `StoreTarget` and `store_target`, and the gate text
    `POSTGRES_NOT_YET` move into `harrier.connection`. (PR #241 did
    this. On 2026-10-11 the ranges here, which cited `pgstore.py` and were
    stale before the move, became the new homes:
    `services/api/src/harrier/connection.py:46-70`, `:152-163`, `:181-221`
    and `:41-43`.) `harrier.pgstore` keeps only
    driver code: connecting, the version reads and the migration runner.
    The CLI and the API import them from `harrier.connection`
    (`harrier_cli/main.py:17`, `harrier_api/app.py:25` today). They are the
    same classes, moved, not new ones. (post-merge review of PRs #194 and
    #207, architecture 6, 2026-10-10)
14. **`scrub_secrets` recognises a Postgres URL's password.**
    `services/api/src/harrier/sources/__init__.py:27-43` scrubs
    `password=` query parameters and Telegram bot paths, not the password
    in a URL's userinfo. Once runs open Postgres, a driver message that
    quotes the URL would reach run summaries and events unscrubbed
    (Behavior). (post-merge review of PRs #194 and #207, privacy 3,
    2026-10-10)
15. **`harrier store migrate` reports from under the lock** (Behavior).
    (post-merge review of PRs #194 and #207, data integrity 4, 2026-10-10)

**Delivery.** One spec, two pull requests in order (Open decision 9).
PR 1 is a refactor with no change in behavior: items 1, 2 (SQLite only), 3,
13, and `with conn:` replaced by `transaction()`. No SQL text and no
exception class changes in it; the suite passes with only annotation,
import and fixture-type edits. PR 2 is the behavior: items 2 (Postgres), 4
to 12, 14 and 15.

## Behavior

**Narrowed (2026-10-11).** Read with the Amendment (2026-10-11): split,
owner and nesting. It replaces "Opened inside another transaction, it
joins that one" with a savepoint, adds the owner's identity to the
Postgres `Connection`, and names which sections below moved to specs 126,
127 and 128.

**Local, URL unset.** Every command, the API and the demo behave as today.
The values read through the API keep their shapes and text. The visible
difference is the class of an exception a store raises, which carries the
driver's exception as its `__cause__`.

**`Connection`.**

- `execute(sql, params=())` takes `?` placeholders on both stores and
  returns a cursor with `fetchone()`, `fetchall()`, iteration and
  `rowcount`. On Postgres the placeholders are rewritten to `%s` outside
  single-quoted literals, and a `%` outside a literal is doubled, so
  nothing in domain SQL is read as a psycopg placeholder.
- `transaction()` is a context manager. It commits on success and rolls
  back on any exception. Opened inside another transaction, it joins that
  one, and the outermost commits. On SQLite it does what `with conn:` does
  today; on Postgres it begins with `SET LOCAL lock_timeout`.
- `Connection` has no `__enter__`. `with conn:` on it is a pyright error,
  so the semantics that close a psycopg connection cannot be written by
  accident.
- `dialect` is `"sqlite"` or `"postgres"`. `busy_timeout_ms` is settable;
  `0` means do not wait on both stores (on Postgres the seam sends 1 ms,
  because `0` there means wait forever).
- `close()` closes the driver connection. Spec 076 holds for both.

**`Row`.** A protocol satisfied by `sqlite3.Row` and by the row a
Postgres `Connection` returns. A row reads by position (`row[0]`), by
column name (`row["id"]`), iterates over its values (`tuple(row)`, as
`tracks.py:261` does), and has `keys()`, so `dict(row)` and
`_job_row_to_dict` work unchanged. Integer columns read as `int` and text
as `str` on both; `NULL` reads as `None`.

**Timestamps.** Every timestamp the domain writes is supplied by Python
from one helper: UTC, `YYYY-MM-DD HH:MM:SS`, the text `datetime('now')`
writes today. Column defaults stay as spec 103 set them, so an insert that
omits a timestamp gets the database's clock in the same format.

**Case folding in dedupe.** On both stores, `find_duplicate` treats
company and title as equal when they are equal after folding `A` to `Z`
to `a` to `z`, and only those. SQLite keeps `COLLATE NOCASE`; Postgres
folds with `translate()` over the 26 letters.

| company in tracker | company added | duplicate on SQLite | on Postgres |
|---|---|---|---|
| `Acme` | `ACME` | yes | yes |
| `École Exemple` | `ÉCOLE EXEMPLE` | no | no |
| `École Exemple` | `école exemple` | yes | yes |

**Text ordering.** Every `ORDER BY` over text orders by code point on both
stores: SQLite's default, and `COLLATE "C"` on Postgres.

**New ids.** `add_job` and `add_contact` read the new id from
`INSERT ... RETURNING id` on both stores. `harrier.db.connect` refuses a
SQLite library older than 3.35, the first with `RETURNING`.

**Upserts.** The three upserts (`runoutcome.py:148`, `userconfig/store.py:171`,
`profile/store.py:33`) name `ON CONFLICT (job)`, `(kind)` and `(kind, name)`.
Spec 105 lands first and makes those keys per owner on Postgres, so no
unique index with exactly those columns exists there, and Postgres refuses
a conflict target it cannot match. Writing `owner_id` into the target would
make domain SQL name a tenant (ADR-013 decision 2). Each becomes, in one
transaction on both stores: update the row; when no row changed, insert it;
when the insert meets a unique violation (another writer inserted first),
run the update once more. One text for both dialects, naming no tenant.

**Store errors.**

| Raised as | SQLite source | Postgres source |
|---|---|---|
| `StoreIntegrityError(kind="unique")` | `UNIQUE constraint failed` | SQLSTATE 23505 |
| `StoreIntegrityError(kind="check")` | `CHECK constraint failed` | 23514 |
| `StoreIntegrityError(kind="foreign_key")` | `FOREIGN KEY constraint failed` | 23503 |
| `StoreIntegrityError(kind="not_null")` | `NOT NULL constraint failed` | 23502 |
| `StoreIntegrityError(kind="refused", message=...)` | a trigger's `RAISE(ABORT, ...)` | P0001 from `harrier_refuse()` |
| `StoreBusyError` | `SQLITE_BUSY` after the timeout | 55P03 after `lock_timeout` |
| `StoreDatabaseError` | any other `sqlite3.Error` | any other `psycopg.Error` |

An unknown track is `kind="refused"`, message `unknown track`, on both:
the seam maps the Postgres foreign key on `jobs.track_id` to the SQLite
trigger's text. After spec 105's migration 10 that key is the composite
`(owner_id, track_id)` reference to `tracks (owner_id, id)`, so the mapping
matches it by the constraint spec 105 names, not by the baseline's
single-column key (note added by spec 105, 2026-10-10). A Postgres error's message is the server's primary
message, never its `DETAIL`, which quotes the row's values. Every probe in
spec 103's parity table that is refused raises the same class, kind and
message on both stores.

Callers see one refusal: `add_job` raises `DuplicateJobError` and
`add_track` raises `DuplicateTrackError` on either store, as they do on
SQLite today.

**The write lock.** The guarantee, on both stores: a decision on a job
(`set_status`), a field write (`update_fields`) and a backfill
(`backfill_events`) read the row, its events and the company's outcomes
only after every earlier writer of that job has committed, and a later
writer of that job reads what this one wrote.

- SQLite: `BEGIN IMMEDIATE`, unchanged. Every write in the file waits.
- Postgres: inside one transaction, the job row is locked first
  (`SELECT id FROM jobs WHERE id = ? AND track_id = ? FOR UPDATE`), and
  every read the write depends on is a later statement. Postgres runs at
  read committed, so each later statement sees every transaction committed
  before it began. Writers of other jobs do not wait.
- Postgres backfill locks the track's jobs that have no events, in id
  order, then reads again, in a new statement, which of them still have
  none, and plans from that second read. Postgres documents that a
  statement which waited for a row lock re-checks its `WHERE` against the
  updated row; this spec does not rely on what that re-check sees in
  `job_events`, so it reads again.
- A job that does not exist, or is in another track, locks no row; the
  read that follows raises `JobNotFoundError` as today.

**The tests on both stores.** A fixture yields a migrated `Connection` per
test, parametrized `sqlite` and `postgres`. The Postgres side migrates one
template database per session with `apply_postgres_migrations` and gives
each test a copy (`CREATE DATABASE ... TEMPLATE`), dropped after it. These
files move their domain tests onto it:

| File | Test functions today |
|---|---|
| `test_tracker_store.py` | 9 |
| `test_tracks.py` | 11 |
| `test_userconfig.py` | 49 |
| `test_profile_store.py` | 4 |
| `test_job_events.py` | 24 |
| `test_tracker_invariants.py` | 26 |
| `test_tracker_queries_name_their_track.py` | 3 |

Counted with `grep -cE '^(async )?def test_'`, so parametrized cases count
once. A test that reads SQLite itself stays SQLite-only: migration
history through raw `sqlite3.connect` (`test_tracks.py:59-175`), `PRAGMA
table_info` (`test_userconfig.py:73`). The PR lists every SQLite-only test
in these files with its reason. `test_runs.py`'s `job_runs` tests run on
both; its migration tests stay SQLite.

The three lock tests run on both:
`services/api/tests/test_job_events.py::test_a_first_move_holds_the_lock_from_read_to_write`,
`services/api/tests/test_job_events.py::test_backfill_holds_the_lock_from_read_to_write`,
`services/api/tests/test_job_events.py::test_a_field_update_holds_the_lock_from_read_to_write`.
Their rival opens with `busy_timeout_ms = 0` and records `waited` when it
gets `StoreBusyError`. A new test proves the Postgres lock is per job: a
decision on one job, held mid-write, does not make a decision on another
job wait on Postgres, and does on SQLite (planned
test_a_decision_on_one_job_does_not_wait_for_another).

**Local-only work on a Postgres connection.** `cutover` and
`migrate_legacy.migrate` raise `StoreLocalOnlyError`: `harrier cutover is
local only: it moves the old system onto the local tracker file`, and
`harrier migrate-legacy is local only: it imports the old system's CSVs
into the local tracker file`. Opening a Postgres store through `open_store`
or `open_postgres_store` takes no host lease, asks the Docker engine
nothing, and writes nothing under `data/` (ADR-011 concerns the SQLite
file).

**The CLI on a Postgres URL.** `harrier store` runs, as in spec 103. Every
other command refuses before it opens anything, takes no lease and hands
nothing to the container, exit 1:

- Local only: `backup`, `restore`, `verify-backup`, `doctor`, `cutover`,
  `migrate-legacy`, `profile import`, `config import`,
  `gmail-migrate-state`, `schedule`, `parity`. Text:
  `error: harrier <command> is local only; it does not run while
  HARRIER_DATABASE_URL names a Postgres store`.
- Every other command. Text: `error: HARRIER_DATABASE_URL names a Postgres
  store; harrier <command> does not run on it. A hosted store is used
  through the API, not the CLI (ADR-013 decision 4)`.

`<command>` is the command and subcommand words only, never an argument,
because arguments carry contact names and free text (spec 061). A test
walks the parser: every top-level command is in exactly one class, and a
new command without a class fails it (planned
test_every_command_has_a_postgres_refusal_class).

**The API on a Postgres URL** refuses to start: `HARRIER_DATABASE_URL
names a Postgres store; the API serves one only once spec 104
authenticates every request`. An unauthenticated API on a shared store is
what ADR-013 decision 3 forbids.

**A Postgres URL's password is scrubbed.** `scrub_secrets`
(`services/api/src/harrier/sources/__init__.py:33-43`) also replaces the
password in the userinfo of a `postgres://` or `postgresql://` URL with
`REDACTED`, keeping the scheme, user, host, port and database. Today it
leaves `postgresql://user:pw@host/db` as written; only a `password=` query
parameter is scrubbed (run against a synthetic URL, 2026-10-10). The
pgstore connection error already scrubs its own message
(`pgstore.py:69-81`); this covers every other text that reaches a run
summary, an event or the exception boundary through `scrub_secrets`.
(post-merge review of PRs #194 and #207, privacy 3, 2026-10-10)

**`harrier store migrate` reports what it found under the lock.**
`apply_postgres_migrations` reads `before` at `pgstore.py:114`, before it
takes the advisory lock at `:119`, so two runs started together on an
empty store each print `postgres 0 -> 10` (`harrier_cli/main.py:1569`)
though each migration was applied once. After this spec the runner reads
`before` under the migration lock and holds a session-level advisory lock
on the same key from that read until its last migration commits. Each
migration still runs in its own transaction, whole or not at all (spec
090). A second run waits, then finds the store at the target and prints
`postgres 10 -> 10`. (post-merge review of PRs #194 and #207, data
integrity 4, 2026-10-10)

## Failure modes

- **SQLite older than 3.35.** `harrier.db.connect` raises `StoreError`:
  `the SQLite library is <version>; harrier needs 3.35 or newer`. The CLI
  prints it and exits 1. Measured on the project interpreter: SQLite
  3.50.4 under CPython 3.12.12
  (`services/api/.venv/bin/python -I -c 'import sqlite3; print(sqlite3.sqlite_version)'`).
  The runtime image (`python:3.12-slim-bookworm`, `Dockerfile:34`) and CI
  were not measured; the PR records both.
- **A writer waits past its timeout.** `StoreBusyError` on both stores, and
  the transaction rolls back. No retry.
- **A deadlock on Postgres.** `StoreDatabaseError`, the transaction rolls
  back. Every lock taken here is one row, or many in id order, so none is
  expected; none is retried.
- **An error inside a Postgres transaction.** Postgres refuses every later
  statement in that transaction. The domain never continues after an error
  inside one: `transaction()` rolls back and re-raises. The best-effort
  identity reads in `logredact.py` run outside write transactions (the
  refresh at `tracker/store.py:704` runs after the commit) and catch
  `StoreDatabaseError`.
- **`with conn:` on a `Connection`.** pyright fails the build.
- **SQL with a `?` or `%` inside a quoted literal.** Left as written on
  Postgres. A `?` outside a literal with no parameter for it raises
  `StoreDatabaseError` on both, as the drivers do today.
- **A Postgres connection lost mid-command.** `StoreDatabaseError`; nothing
  retries, and an open transaction is lost whole.
- **A local-only function given a Postgres connection.**
  `StoreLocalOnlyError` before any statement runs.
- **A data command or the API with a Postgres URL.** The refusals under
  Behavior, exit 1, nothing opened.
- **Postgres tests without a server.** Skipped locally, failed in CI
  (`CI=true`), as spec 103 and spec 039 require.
- **A driver or library message quotes a Postgres URL.** The password is
  `REDACTED` wherever `scrub_secrets` runs; the host and database stay,
  so the message still says which store failed. (post-merge review of
  PRs #194 and #207, 2026-10-10)
- **Two `harrier store migrate` runs at once.** The second waits for the
  first's lock, applies nothing, and reports the version it found under
  the lock. Neither reports a migration it did not apply. A run killed
  while holding the session lock releases it when its connection closes.
  (post-merge review of PRs #194 and #207, 2026-10-10)
- **A module other than `harrier.pgstore` or `harrier.connection` imports
  `psycopg`.** `lint-imports` fails, and so does `just check`.
  (post-merge review of PRs #194 and #207, 2026-10-10)

## Acceptance criteria

**Narrowed (2026-10-11).** PR 1's criteria stand. PR 2's list below is
replaced by the criteria under the Amendment (2026-10-11): split, owner
and nesting; each criterion below that this spec no longer carries is
listed there with the spec that now carries it.

PR 1 (refactor):

- [ ] `harrier.connection.Connection` and `Row` exist, and
      `harrier.db.connect` returns a `Connection`.
- [ ] `grep -rn 'sqlite3\.Connection' services/api/src` matches only
      `harrier/connection.py` and `harrier/db.py`.
- [ ] No `with conn:` remains in `services/api/src`; each became
      `transaction(conn)` or the connection's `transaction()`.
- [ ] The PR shows pyright failing on a `with conn:` over a `Connection`.
- [ ] The full suite passes; the only test edits are annotations and
      fixture types, and the PR lists the files.
- [ ] No SQL text and no exception class changes in this PR.
- [ ] `StoreError` and its subclasses, `StoreTarget`, `store_target` and
      `POSTGRES_NOT_YET` are defined in `harrier.connection`, not in
      `harrier.pgstore`, and no module under `services/api/src` imports
      them from `harrier.pgstore`
      (`services/api/tests/test_connection.py::test_store_neutral_names_live_in_connection`).
      (post-merge review of PRs #194 and #207, 2026-10-10)

PR 2 (behavior):

- [ ] The three upserts are update-then-insert with one retry, pass on
      both stores, and two writers inserting the same key at once leave
      one row on both (planned test_an_upsert_racing_another_leaves_one_row).
- [ ] Every test in the seven files under Behavior that is not listed as
      SQLite-only runs with both parameters, and the CI log of
      `check-python` shows the Postgres parameters passing, not skipped.
- [ ] The three lock tests named under Behavior pass on both stores.
- [ ] A decision on one job does not wait for another on Postgres and
      waits on SQLite (planned
      test_a_decision_on_one_job_does_not_wait_for_another).
- [ ] A row reads by position, by name, as values and as a dict on both
      (`services/api/tests/test_seam_on_both_stores.py::test_a_row_reads_by_position_by_name_and_as_a_dict`).
- [ ] A `?` statement binds on both, including one whose literal holds a
      `%`
      (`services/api/tests/test_seam_on_both_stores.py::test_question_marks_bind_on_both_stores`).
- [ ] A nested `transaction()` joins the outer one, and an exception in
      either rolls both back (planned
      test_a_nested_transaction_joins_the_outer_one).
- [ ] `updated_at` written by `set_status`, `update_fields`,
      `archive_track`, `set_config` and `put_document` matches
      `^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$` on both (planned
      test_a_written_timestamp_has_one_shape_on_both_stores).
- [ ] The three rows of the folding table hold on both (planned
      test_dedupe_folds_ascii_case_only_on_both_stores).
- [ ] Text orders by code point on both, and `LIMIT 1` by name picks the
      same document (planned test_text_orders_by_code_point_on_both_stores).
- [ ] `add_job` and `add_contact` return the inserted id on both (planned
      test_an_insert_returns_its_id_on_both_stores).
- [ ] A SQLite library below 3.35 is refused with the text under Failure
      modes
      (`services/api/tests/test_seam_on_both_stores.py::test_an_old_sqlite_library_is_refused`).
- [ ] Every refused probe of spec 103's parity table raises the same class,
      kind and message on both, `unknown track` included
      (`services/api/tests/test_seam_on_both_stores.py::test_every_parity_probe_raises_one_error_on_both_stores`).
- [ ] A duplicate track slug is `DuplicateTrackError`, and a unique-index
      race in `add_job` is `DuplicateJobError`, on both (planned
      test_a_duplicate_is_refused_alike_on_both_stores).
- [ ] A Postgres integrity error's message holds no `DETAIL` text
      (`services/api/tests/test_seam_on_both_stores.py::test_a_postgres_refusal_never_quotes_the_row`).
- [ ] A writer held past a short timeout gets `StoreBusyError` on both, and
      a Postgres `busy_timeout_ms` of 0 does not wait forever
      (`services/api/tests/test_seam_on_both_stores.py::test_a_stuck_writer_reports_busy_on_both_stores`).
- [ ] Opening a Postgres store writes no lease and makes no engine query
      (`services/api/tests/test_seam_on_both_stores.py::test_opening_a_postgres_store_takes_no_lease`).
- [ ] `cutover` and `migrate_legacy.migrate` refuse a Postgres connection
      with the texts under Behavior (planned
      test_local_only_work_refuses_a_postgres_connection).
- [ ] The identity loaders in `logredact.py` return what they can when a
      store read fails, on both
      (`services/api/tests/test_seam_on_both_stores.py::test_identity_reads_survive_a_store_error_on_both_stores`).
- [ ] Every top-level command has one refusal class, and the two texts
      and the API's text are as under Behavior (planned
      test_every_command_has_a_postgres_refusal_class). Spec 103's
      refusal tests in `services/api/tests/test_store_cli.py` are updated
      to the new texts.
- [ ] The import-linter contract fails when a third module imports
      `psycopg` (recorded in the PR).
- [ ] The import-linter contract confining `psycopg` to `harrier.pgstore`
      and `harrier.connection` is in `services/api/pyproject.toml`, and
      `lint-imports` in `just check` passes with it.
      (post-merge review of PRs #194 and #207, 2026-10-10)
- [ ] `scrub_secrets` replaces the password in a `postgresql://` and a
      `postgres://` URL's userinfo with `REDACTED` and keeps user, host,
      port and database; a URL with no password is unchanged
      (`services/api/tests/test_api_exposure.py::test_scrub_secrets_hides_a_postgres_url_password`,
      built under spec 128).
      (post-merge review of PRs #194 and #207, 2026-10-10)
- [ ] Two `harrier store migrate` runs started together on an empty
      Postgres store apply each migration once, one reports
      `postgres 0 -> <target>` and the other `postgres <target> -> <target>`
      (`services/api/tests/test_store_cli.py::test_concurrent_migrate_runs_report_what_each_found`,
      built under spec 128).
      (post-merge review of PRs #194 and #207, 2026-10-10)
- [ ] No log record and no HTTP error detail carries the store URL's
      password. Domain code on a Postgres store can raise a driver error
      that quotes the URL, and two sinks do not run `scrub_secrets`: the
      log stream, whose `IdentityRedactionFilter`
      (`services/api/src/harrier/logredact.py`) redacts identity values
      only, and the API routes that answer `HTTPException(detail=str(error))`
      (in `services/api/src/harrier_api/`). With a password generated at
      run time in `HARRIER_DATABASE_URL`, an error raised through each sink
      leaves no 8-character window of it
      (planned test_no_log_or_http_error_carries_the_store_password).
      (privacy review of PR #281, 2026-10-11; recorded here from spec 128)
- [ ] `services/api/src/harrier/tracker/schema.py` and
      `packages/contract` do not change.
- [ ] `docs/architecture.md` describes the domain on either store and no
      longer says "until spec 112".
- [ ] `just check` passes.
- [ ] No domain insert supplies an `id`; every new id comes from the store.
      An explicit id would refuse on another owner's row with the primary
      key's message and so reveal that the id is taken (planned
      test_no_domain_insert_supplies_an_id). Spec 105's follow-up makes an
      explicit id a refusal on Postgres (post-merge review of PR #216,
      2026-10-10).

## Honest limitations

- After this spec, nothing outside the tests reads or writes tracker rows
  on Postgres. The CLI runs only `harrier store` there, and the API waits
  for spec 104. The proof is the suite run on both stores in CI.
- Locally the lock is still the whole file: a decision on one job waits
  for a decision on another. That is today's behavior and stays.
- The Postgres guarantee covers the three locked writers. Inserts
  (`add_job`, `add_contact`, `add_track`) and upserts rely on unique
  constraints, on both stores, as SQLite's deferred transactions do today.
- Written timestamps come from the process's clock, defaults from the
  database's. Hosted, those are two machines; a skew between them can
  order a row's `created_at` after its `updated_at`.
- `set_status` and `add_job` hash the description cached under the
  process's `data/` directory (`tracker/store.py:495-508`). On a hosted
  store that cache is spec 107's.
- ASCII-only folding keeps SQLite's rule on both stores. The screening
  dedupe folds with Python's `str.lower()` (`screening/normalized.py:37-38`),
  which folds `É` too. The two rules already disagree locally; this spec
  does not reconcile them.
- The placeholder rewrite is a small tokenizer, not a SQL parser. It knows
  single-quoted literals only. Domain SQL today has no double-quoted
  identifiers, comments or dollar quotes holding a `?`.
- Postgres connection pooling, and how `SET LOCAL` behaves behind
  Supabase's pooler, are spec 110's. `SET LOCAL` was chosen because it
  ends with the transaction.
- CI time grows by the Postgres parameters. Not measured; the PR records
  `check-python`'s duration before and after.

## Migration

None. No schema change, no data change, no contract change. A local
tracker file opens exactly as before.

## Options weighed

- **A protocol over the raw driver connections.** `sqlite3.Connection`
  already fits most of one. It cannot rewrite placeholders or map errors,
  so every call site would carry both concerns. Not chosen (Open decision
  1).
- **`%s` as the canonical placeholder, rewritten to `?` for SQLite.** The
  same rewriter in the other direction, and every existing statement
  edited. Not chosen.
- **SQLAlchemy Core.** Rejected by spec 103 for the store; the same reasons
  hold for the domain.
- **psycopg's `dict_row`.** Breaks `row[0]` and makes `tuple(row)` yield
  column names. Not chosen.
- **A store-wide advisory lock on Postgres.** The nearest thing to
  `BEGIN IMMEDIATE`, and it makes every owner wait for every other. Not
  chosen.
- **Serializable isolation with retries.** No explicit lock, but every
  writer needs a retry loop, and a retry re-reads the description cache
  file. Not chosen.
- **Every test file on both stores.** Many tests read SQLite itself
  (pragmas, `sqlite_master`, raw migration history). Not chosen: the seven
  files hold the domain's behavior.

## Open decisions for Akin

Akin took the recommended answer to each on 2026-10-10. Scope and
Behavior above describe those answers. The build order taken with spec
105 the same day (103, 105, 112, 104) is why this spec depends on 105 and
rewrites the three upserts.

1. **Seam shape.** Recommended: a harrier-owned `Connection` wrapping
   either driver, with placeholder rewriting and error mapping in one
   place. Alternative: a protocol that raw `sqlite3.Connection` satisfies,
   plus a Postgres adapter, with errors mapped at each catch site.
2. **Timestamps.** Recommended: Python supplies them, one helper, one
   format, testable with a fixed clock. Alternative: a per-store SQL
   expression (`datetime('now')`, and spec 103's `to_char` expression), so
   the database's clock writes every timestamp.
3. **Case folding.** Recommended: ASCII-only on both, SQLite's rule
   today. Alternatives: `lower()` on Postgres, accepting that the stores
   disagree on accented names; or Unicode folding on both, which changes
   local dedupe and is its own spec.
4. **Text ordering.** Recommended: code point on both (`COLLATE "C"` on
   Postgres). Alternative: accept each database's collation, and the two
   `LIMIT 1` reads choosing differently.
5. **New ids.** Recommended: `RETURNING id` on both, with the 3.35 check.
   Alternative: `lastrowid` on SQLite and `RETURNING` on Postgres, no
   version check, two code paths.
6. **The Postgres write lock.** Recommended: lock the job row
   (`FOR UPDATE`) before reading. It is scoped to the row, released at
   commit, and needs no key space. Alternative:
   `pg_advisory_xact_lock` keyed by job id; it locks a key rather than a
   row, needs a namespace apart from spec 103's migration key, and the
   two-integer form cannot hold a `bigint` id.
7. **Error mapping breadth.** Recommended: every driver error through a
   `Connection` becomes a store error, the driver's as its cause.
   Alternative: map integrity and busy errors only and let the rest pass
   as driver classes, which leaves `logredact.py` and `logsetup.py`
   catching two families.
8. **The CLI on a Postgres URL.** Recommended: only `harrier store` runs;
   every other command refuses with its class. ADR-013 decision 4 says
   the hosted deployment has no tenant CLI. A command run on a Postgres URL
   has no authenticated owner, so with spec 105 in place it would act
   either as nobody (refused by the policy) or as the service role (which
   bypasses it). `harrier add` also writes the local description cache
   (`capture.py:108`, `:155`). Alternative: lift the gate for commands
   whose only durable effect is in the store (`tracks`, the status verbs,
   `company-outcome`, `events`, `check`, `next`, `review`,
   `config list|get|set|unset`, `profile list`, `contacts list`,
   `outreach sync|due|mark-sent|mark-replied|snooze`, `export`), as a
   development aid with an explicit owner for the session. That alternative
   also needs logging setup to load the redaction values from the Postgres
   store (`logsetup.py:138` opens SQLite), or a hosted identity would go
   unredacted in logs.
9. **Delivery.** Recommended: this one spec, two pull requests, PR 1 a
   refactor with no behavior change, PR 2 the behavior. The annotation
   change cannot be separated from the new type without breaking pyright,
   and alone it specifies nothing. Alternative: split into two specs, a
   connection-type spec landing first and this one depending on it.
10. **Postgres test databases.** Recommended: one migrated template per
    session, a copy per test. Alternative: migrate a fresh database per
    test, as spec 103's tests do, which is slower by one baseline per test.

## Proof / origin

- `specs/103-the-tracker-store-speaks-postgres.md`: Problem, the parity
  table, Honest limitations (`lower()` and `NOCASE`), and Out of scope.
- `docs/adr/ADR-013-hosted-multi-tenant-deployment.md`, decisions 3, 4
  and 5.
- `docs/adr/ADR-003-tracker-store.md` and
  `docs/adr/ADR-012-search-tracks-and-tenant-isolation.md` point 4: one
  write path per store.
- `docs/adr/ADR-011-one-kernel-owns-the-database.md`: the host lease and
  container ownership concern the SQLite file.
- `specs/079-decisions-are-recorded-as-events.md`, amendment of the review
  of the fixes: the write lock and its three tests.
- `specs/076-connections-close-when-their-caller-is-done.md`: what
  `with conn:` does and does not do.
- `specs/090-migrations-apply-whole-or-not-at-all.md`: transactions per
  migration, which `db.py` keeps.
- `specs/061-one-kernel-owns-the-database.md` and
  `specs/075-host-lease-holds-the-database.md`: refusals name the command,
  never its arguments.
- `specs/039-gates-that-can-fail.md`: a gate that skips in CI has not run.
- `services/api/src/harrier/tracker/store.py`, `tracks.py`,
  `userconfig/store.py`, `profile/store.py`, `runoutcome.py`,
  `logredact.py`, `apply/profile.py`, `resume/content.py`: every site
  listed under Problem.
- `services/api/tests/test_job_events.py` and
  `services/api/tests/test_concurrent_writers.py`: the lock and busy
  tests.

## Out of scope

- Authentication, the request role (`harrier_tenant`, spec 105's
  Amendment), and the API on Postgres: spec 104.
- `owner_id` and row-level policy: spec 105.
- Per-owner credentials and run isolation: spec 106.
- Files under `data/` (the description cache, scoring models, artifacts,
  seen-state) on a hosted store: spec 107.
- Connection pooling, Supabase's pooler, and the runtime image installing
  psycopg: spec 110.
- A tenant export: spec 111. `harrier export` stays local.
- Native timestamp types, and any schema change.
- Found while drafting, each for its own spec:
  - Screening dedupe folds Unicode case (`screening/normalized.py:37-38`)
    and the tracker folds ASCII only (`tracker/store.py:105`), so a pair
    screening calls new can be refused by `add_job`, and the reverse.
  - `add_job` maps every integrity refusal to `DuplicateJobError`
    (`tracker/store.py:182-186`), a check or unknown-track refusal
    included. Unreachable today, since the scope's track exists and the
    fields are validated first; kept as is here so both stores behave the
    same.

## Amendment (2026-10-11): operator and run classes

**Moved (2026-10-11).** This amendment now lives in spec 127, and its
effect here is replaced by decision 4 of the Amendment (2026-10-11):
split, owner and nesting. Until spec 127 lands, only `harrier store` runs
on a Postgres URL. The text below stands as the record of what was
decided.

Akin took this in chat on 2026-10-11 ("go with your recommendations"),
with specs 106, 108 and 111, which asked it of this spec. It changes "The
CLI on a Postgres URL" and nothing else here. Where Scope item 9,
Behavior, Honest limitations and Open decision 8 say that only `harrier
store` runs on a Postgres URL, read this amendment.

The gate gains exactly two classes beyond `harrier store`:

| Class | Commands | On a Postgres URL |
|---|---|---|
| `harrier store` (spec 103) | `store` and its subcommands | runs |
| operator (specs 108, 111) | `harrier operator`, one command group: `scheduler` and `schedule-report` (spec 108), `invite` and `erase` (spec 111) | runs; each subcommand runs as the database role it declares |
| run command, owner required (spec 106) | the verbs of the run kinds spec 106 runs hosted | runs only with `HARRIER_RUN_OWNER` naming an owner; otherwise refused with spec 106's text |
| local only | as Behavior lists, plus the commands spec 109 moves there | refused, Behavior's text |
| everything else | the rest | refused, Behavior's text |

- **One operator group.** Every operator subcommand is a subcommand of
  `harrier operator`. No other top-level command is an operator command.
- **Each subcommand declares its role.** The declaration sits beside the
  parser, where the parser walk reads it, and names the database role the
  subcommand runs as, or that it opens no database connection. Specs 108
  and 111 state each one. A subcommand without a declaration fails the
  walk.
- **On SQLite,** `harrier operator` refuses before it opens anything, with
  spec 108's text.
- The run class's rules (no `.env` read, `HARRIER_RUN_OWNER` a UUID, never
  beside SQLite) are spec 106's.

### Acceptance criteria (amendment of 2026-10-11)

- [ ] Walking the parser, every command and subcommand is in exactly one
      class of the table above; every `harrier operator` subcommand
      declares a database role or declares that it opens none, and a
      subcommand added without a declaration fails the walk; a run command
      on a Postgres URL without `HARRIER_RUN_OWNER` is refused with spec
      106's text and opens nothing
      (planned test_the_postgres_gate_has_operator_and_run_classes).
      Planned test_every_command_has_a_postgres_refusal_class covers the
      same walk.

## Amendment (2026-10-11): what PR 1 built

Implementing PR 1 settled choices this spec left open. Each one is stated
here so the code and the spec agree. None changes behavior a user sees;
PR 2 still owns everything Behavior lists for Postgres.

- **`transaction()` is a method only.** `with conn.transaction():` is the
  one spelling. No module function exists.
- **Nested `transaction()` joins in PR 2, not PR 1.** In PR 1 it is the
  sqlite3 connection's own context manager, exactly what `with conn:` did.
  Joining now would move the commit inside `_write_lock`
  (`tracker/store.py`) from the inner block to `_write_lock`, a behavior
  change PR 1 may not make. No `transaction()` nests inside another today.
  Joining arrives with PR 2 and its planned test
  test_a_nested_transaction_joins_the_outer_one.
- **`Connection` keeps `commit()`, `rollback()` and `in_transaction`.**
  `_write_lock` uses all three and `runoutcome.record_success` calls
  `commit()`. PR 2 decides whether they survive.
- **`dialect` and `busy_timeout_ms` arrive with PR 2.** Nothing in PR 1
  reads them.
- **`Connection.sqlite_driver`** returns the sqlite3 connection underneath,
  for code whose subject is SQLite itself. Only three test modules use it:
  `test_api_tracks.py` (statement tracing), `test_dialect_parity.py`
  (autocommit for the SQLite probes) and `test_migration_runner.py`
  (transaction mode). No module under `src` uses it.
- **Cursor reads stay `Any`.** `execute()` returns a `Cursor` protocol
  whose `fetchone()` and `fetchall()` are typed as `sqlite3.Cursor` types
  them today. Narrowing them would force edits at every unchecked
  `fetchone()[0]`, which PR 1 forbids. `Cursor` carries `lastrowid` until
  PR 2 moves `add_job` and `add_contact` to `RETURNING id`.
- **`Row` replaces `sqlite3.Row` in two annotations only:**
  `_job_row_to_dict` (`tracker/store.py`) and `_track_from_row`
  (`tracks.py`).
- **More moved to `harrier.connection` than Scope item 13 lists.**
  `store_target` needs `URL_VARIABLE`, `POSTGRES_SCHEME` and the URL
  parsing helpers, and `harrier.connection` cannot import
  `harrier.pgstore` at import time. So those moved too, with the parser
  made public as `UrlParts` and `url_parts` because `pgstore` still uses
  it. `StoreMigrationRefused` (spec 105) moved with the other `StoreError`
  subclasses.
- **`db.schema_version` takes a `Connection` or a raw sqlite3
  connection.** The migration runner calls it before wrapping.
- **Test edits beyond annotations.** Six test modules needed more than an
  annotation because pyright refuses the old form: `test_ui_settings.py`
  and `test_migration_runner.py` (`with conn:` became
  `with conn.transaction():`), `test_migration_runner.py`,
  `test_api_tracks.py` and `test_dialect_parity.py` (through
  `sqlite_driver`), and `test_backup.py` and `test_logging.py` (a
  hand-opened sqlite3 connection is wrapped in `Connection` before a domain
  reader gets it).

`services/api/tests/test_connection.py::test_store_neutral_names_live_in_connection`
proves the moved names live in `harrier.connection`.

### Note (2026-10-11): post-merge review of PR #241

Two local reviews of PR #241 after it merged found gaps in the record
above and in its tests. The text above stands as written; this note
corrects it.

- **Test edits the list above misses.** Three test modules imported a
  moved name from `harrier.pgstore` and now import it from
  `harrier.connection`: `test_postgres_store.py` and `test_owner_policy.py`
  split their import between the two modules, and `test_store_cli.py`
  moved its import whole. `test_database_ownership.py` changed annotations
  only, but annotated its stand-ins for `sqlite3.connect` as returning
  `Connection`, which none of them does. The follow-up makes the two that
  always raise return `NoReturn` and the one that returns the real
  connection return `sqlite3.Connection`, which lets it drop its
  `type: ignore`.
- **The count of `with conn:` sites** under Problem was 14, not 13. The
  line there is corrected and says so.
- **Moved names.** The import guard's set of moved names now includes the
  ones this amendment adds (`URL_VARIABLE`, `POSTGRES_SCHEME`, `UrlParts`,
  `url_parts`), not only Scope item 13's.

The follow-up adds these proofs:

- `Connection` has no `__enter__` or `__exit__` (nor the async pair), so
  pyright keeps refusing `with conn:`
  (`services/api/tests/test_connection.py::test_a_connection_offers_no_with_block`).
- No module under `services/api/src` reads `sqlite_driver`, as an
  attribute or through `getattr`
  (`services/api/tests/test_connection.py::test_no_module_under_src_reaches_the_sqlite_driver`),
  and the check finds each form
  (`services/api/tests/test_connection.py::test_the_driver_check_finds_every_read`).
- An exception inside `transaction()` rolls the write back and leaves the
  connection open and usable; a clean exit commits, seen from a second
  connection
  (`services/api/tests/test_connection.py::test_a_transaction_rolls_back_on_error_and_commits_on_success`).
- The import guard resolves a relative import against the file's package,
  so `from .pgstore import StoreError` in `harrier` is caught
  (`services/api/tests/test_connection.py::test_the_import_check_resolves_relative_imports`).
- `services/api/tests/test_connection.py::test_store_neutral_names_live_in_connection`
  now fails when its scan did not read `harrier_cli/main.py`,
  `harrier_api/app.py` and `tests/test_store_cli.py`, so a wrong root no
  longer passes by finding nothing. It also fails when a moved name that
  `harrier.pgstore` holds is not the very object `harrier.connection`
  defines.

### Note (2026-10-11): post-merge review of PR #244

A local review of PR #244 after it merged found that the last proof above
says more than its check did. The text above stands as written; this
note corrects it.

- **`is` cannot see a same-valued string.** The test compared each moved
  name `harrier.pgstore` holds with `harrier.connection`'s by `is`. The
  moved names `URL_VARIABLE` and `POSTGRES_SCHEME` are interned strings,
  so `URL_VARIABLE = "HARRIER_DATABASE_URL"` written in `pgstore.py` is
  the same object, and the test passed with it. `POSTGRES_SCHEME` was
  also missing from the list of names `pgstore` must not hold.
- **The test now reads `pgstore.py`.** Every moved name it binds must
  come from `from harrier.connection import` under its own name: never an
  assignment, a `def`, a `class`, another import, or any other binding.
  The names `pgstore` must not hold are every moved name it does not
  import from the seam, so `POSTGRES_SCHEME` is among them without a
  list to keep
  (`services/api/tests/test_connection.py::test_store_neutral_names_live_in_connection`).
  With `URL_VARIABLE = "HARRIER_DATABASE_URL"` or `POSTGRES_SCHEME =
  "postgresql"` added to `pgstore.py`, it fails. The check finds each
  binding form
  (`services/api/tests/test_connection.py::test_the_local_binding_check_finds_every_form`).
- **Stale line numbers.** Problem's citations of the gate were stale:
  they are now `harrier_cli/main.py:3079-3082`, `harrier_api/app.py:1370-1371`
  and `connection.py:42-44`. Scope item 13's ranges are a dated record of
  where the names went on 2026-10-11 and stay as written.

## Amendment (2026-10-11): split, owner and nesting

An agent preparing PR 2 found it too large to review against this spec.
Its analysis of 2026-10-11 counted about 45 files: the Postgres seam; the
`execute(` sites of eight domain modules and the write lock's rewrite;
the catch sites in `src` and the test files naming `sqlite3` error
classes; a dual-store fixture and the conversion of seven test files and
`test_runs.py`; a restructure of `harrier_cli/main.py` into five gate
classes; three review findings unrelated to the domain; and about 22 new
planned tests. The rule for that case is to split the spec, not the
branch (CLAUDE.md, change-boundary). The same analysis found five gaps,
answered below.

### Decisions (Akin, in chat, 2026-10-11)

1. **Split the spec as proposed.** A, the seam speaks both dialects, is
   this spec. B, domain SQL is portable, is
   `specs/126-domain-sql-is-portable.md`. C, the CLI and API gates name
   their classes, is
   `specs/127-the-cli-and-api-gates-name-their-classes.md`. D, the three
   review findings on PRs #194 and #207, is
   `specs/128-three-postgres-store-review-findings.md`. This file keeps its
   name and title, so the citations other specs make of it hold.
2. **Owner on Postgres: the seam sets role and claims.**
   `open_store(owner=...)` runs `SET LOCAL ROLE harrier_tenant` and sets
   `request.jwt.claims` with `set_config(..., true)` at the start of every
   transaction. That is the path spec 106's runs and spec 104's requests
   use. The domain's Postgres tests run under the real row policy:
   migration 16 makes `owner_id` default from `public.harrier_owner_id()`,
   which reads only `request.jwt.claims`
   (`services/api/src/harrier/tracker/schema.py:1338-1344`, `:1351-1354`).
3. **Nesting: savepoint.** A `transaction()` opened inside another is a
   SAVEPOINT on both stores. An inner error the domain catches rolls back
   only the inner part, and the outer transaction stays usable on
   Postgres. This replaces "Opened inside another transaction, it joins
   that one" under Behavior, and the PR 1 amendment's "Nested
   `transaction()` joins in PR 2". It matches spec 104 (Behavior, the
   request transaction, step 4; Open decision 3).
4. **Gate classes deferred to spec 127.** Until spec 127 lands, only
   `harrier store` runs on a Postgres URL. Run commands refuse even with
   `HARRIER_RUN_OWNER`, and no empty `harrier operator` group ships. Spec
   127 is sequenced after specs 106, 108 and 111 build their commands.
   This replaces the effect of the Amendment (2026-10-11): operator and
   run classes, which moved to spec 127 whole.

### Gaps the analysis found, and where each is answered

1. **No owner identity on Postgres.** Decision 2, under "The owner's
   identity" below.
2. **Four upserts, not three.** `profile/store.py` has two since spec 099,
   with partial-index conflict targets (migration 11). Spec 126 rewrites
   all four.
3. **Nested transactions disagreed with spec 104.** Decision 3.
4. **The gate classes pointed at commands that do not exist.** There is
   no `harrier operator` group in `harrier_cli/main.py` today, and the run
   class needs spec 106's owner. Decision 4.
5. **Spec 115 asked that this spec's `depends` gain 115**
   (`specs/115-spec-105-holds-on-a-real-supabase-project.md`, Changes to
   approved specs). The frontmatter now carries it. This spec's PR 2 is
   built after spec 115's acceptance, as Akin decided on 2026-10-10. Spec
   126 is gated the same way; spec 128 is not, because it changes no
   domain code. Several line numbers under Problem were stale; the
   citations in this amendment are from `main` at `87c3926` on
   2026-10-11, and Problem stands as a dated record.

### Where each item went

| Item of this spec | Now in |
|---|---|
| Scope 1, one connection type | this spec (PR 1 built the SQLite side) |
| Scope 2, two openers and `open_store()` | this spec, with the owner (decision 2) |
| Scope 3, the annotations | this spec (PR 1 built it) |
| Scope 4, portable SQL: placeholders | this spec |
| Scope 4: timestamps, case folding, text ordering, new ids, upserts | spec 126 |
| Scope 4: transactions | this spec (PR 1 replaced `with conn:`; nesting is decision 3) |
| Scope 5, one set of store errors, and the catch sites | this spec |
| Scope 6, the write lock per store | spec 126 |
| Scope 7, a bounded wait on both | this spec |
| Scope 8, local-only functions refuse a Postgres connection | spec 126 |
| Scope 9, the gate's refusals | spec 127 |
| Scope 10, the tests on both stores | spec 126; the seam's own Postgres tests stay here |
| Scope 11, psycopg has two importers | spec 128 |
| Scope 12, docs | spec 126 |
| Scope 13, store-neutral names leave `pgstore.py` | this spec (PR 1 built it) |
| Scope 14, `scrub_secrets` and a Postgres URL | spec 128 |
| Scope 15, `harrier store migrate` reports from under the lock | spec 128 |
| Behavior: Local, URL unset; `Connection`; `Row`; Store errors | this spec |
| Behavior: New ids, the 3.35 check | this spec |
| Behavior: New ids, `RETURNING id`; Timestamps; Case folding; Text ordering; Upserts; The write lock; The tests on both stores | spec 126 |
| Behavior: Local-only work, the refusals of `cutover` and `migrate_legacy.migrate` | spec 126 |
| Behavior: Local-only work, opening a Postgres store takes no lease | this spec |
| Behavior: The CLI on a Postgres URL; The API on a Postgres URL | spec 127 |
| Behavior: A Postgres URL's password is scrubbed; `harrier store migrate` reports what it found under the lock | spec 128 |
| Amendment (2026-10-11): operator and run classes | spec 127 |
| Open decision 8 (the CLI on a Postgres URL) | spec 127 |
| Open decision 10 (Postgres test databases) | spec 126 |

Other specs cite this one for things that moved. Their text is not edited
here. Read "spec 112" in them as follows: the update-then-insert upsert
shape (specs 108 and 116) is spec 126's; the CLI's Postgres classes and
refusal texts (specs 106, 108, 109 and 111) are spec 127's; the
session-level migration lock (spec 110, Open decision 8) is spec 128's.
`Connection.dialect` (specs 108 and 109), `open_store(owner=...)` (spec
106) and the savepoint (spec 104) stay here.

### What PR 2 of this spec delivers

**`open_store()`.** `harrier.connection.open_store(environ=None, *,
owner=None)` opens the store `store_target(environ)` names and returns a
`Connection`.

- SQLite, no owner: `harrier.db.connect()`, unchanged.
- SQLite with an owner: `StoreError` `an owner applies only to a Postgres
  store`, and nothing opens. Spec 106 refuses `HARRIER_RUN_OWNER` beside a
  SQLite store for the same reason.
- An owner that is not a UUID: `StoreError` `the owner is not an owner
  id`, and nothing opens.
- Postgres: `harrier.pgstore.open_postgres_store(url)`, with spec 103's
  version check, wrapped in a Postgres `Connection`. It takes no host
  lease, asks the Docker engine nothing and writes nothing under `data/`
  (ADR-011 concerns the SQLite file).
- psycopg not installed: `StoreDriverMissing`, as today.

**The Postgres `Connection`.** Behavior's `Connection` and `Row` bullets
hold on Postgres, including the placeholder rewrite. `close()` closes the
psycopg connection. `sqlite_driver` raises `StoreError` `this connection
is not SQLite`. The driver connection is in autocommit mode
(`pgstore.py:125`), and the seam opens every transaction itself: a
statement issued outside `transaction()` runs in a transaction of its own
with the same first statements, so no statement runs without them.

**Every Postgres transaction starts the same way.** The outermost
transaction's first statement is `SET LOCAL lock_timeout = '<n>ms'`, where
`n` is the connection's `busy_timeout_ms`, or 1 when that is 0, because
`0` on Postgres means no limit. `SET LOCAL` ends with the transaction, so
nothing carries over to another client behind a transaction-mode pooler.

**The owner's identity (decision 2).** On a connection opened with an
owner, every outermost transaction then, before any other statement:

1. runs `SET LOCAL ROLE harrier_tenant`;
2. runs `SELECT set_config('request.jwt.claims', <claims>, true)`, where
   the claims are `{"sub": "<owner>", "role": "authenticated", "aud":
   "authenticated"}` (spec 106, "The run's database session");
3. reads back `current_user` and `current_setting('request.jwt.claims',
   true)::jsonb ->> 'sub'`. When either differs from what it set, the
   transaction rolls back and raises `StoreDatabaseError` `the run's
   database identity did not hold` (spec 106's text), and no other
   statement runs.

So every insert into an owned table takes the owner's id as its
`owner_id` default, and the `owner_only` policies limit every read and
write to the owner's rows. Domain SQL names no tenant (ADR-013 decision
2). The three steps are one function in `harrier.connection` that takes
the claims to set. `open_store(owner=...)` passes spec 106's three.
Spec 104's request transaction passes the request's verified claims to the
same function; how spec 104 obtains and pools its connections stays spec
104's.

A Postgres connection opened without an owner runs as its login role,
with no role switch and no claims. `harrier_owner_id()` is then null, and
an insert into an owned table is refused
(`services/api/tests/test_owner_policy.py::test_an_insert_without_an_owner_is_refused`
shows the store refusing an insert that has no owner). It exists for the
seam's own tests and for spec 127's operator roles.

**`dialect` and `busy_timeout_ms`.** `dialect` is `"sqlite"` or
`"postgres"`, read only. `busy_timeout_ms` defaults to `BUSY_TIMEOUT_MS`
(`db.py:33`) and is settable. On SQLite, setting it sets `PRAGMA
busy_timeout`, and `0` means do not wait. On Postgres it sets the
`lock_timeout` of every later transaction, `0` sending 1 ms.

**Nesting (decision 3).** A `transaction()` opened while another is open
on the same connection is a SAVEPOINT, on both stores.

- The inner block exits normally: the savepoint is released. Nothing
  commits until the outermost block exits.
- The inner block raises: its work rolls back to the savepoint and the
  exception propagates. When the domain catches it inside the outer
  block, the outer transaction goes on without the inner work. On
  Postgres it stays usable, because rolling back to a savepoint ends the
  aborted state. When the exception leaves the outer block, everything
  rolls back.
- The outermost block commits on success and rolls back on any
  exception.
- On an owner connection the identity steps run once, at the start of the
  outermost transaction; `SET LOCAL` holds for its savepoints too.

Spec 126's upserts depend on this: an insert that meets a unique
violation is caught and the update runs again, which Postgres allows only
when the insert ran in a savepoint.

**`commit()`, `rollback()` and `in_transaction`.** `commit()` and
`rollback()` leave `Connection`. `in_transaction` stays, read only, on
both stores. Their two users change:

- `_write_lock` (`tracker/store.py:309-330`) becomes
  `transaction(immediate=True)`. On SQLite the outermost such transaction
  begins with `BEGIN IMMEDIATE`, as today; nested, it is a savepoint and
  the caller keeps the lock it holds, as today. On Postgres `immediate`
  adds nothing in this spec; spec 126 adds the per-job row lock. Each of
  the four writers under the lock (`set_status`, `update_fields`,
  `backfill_events` and spec 122's `record_eligibility`) ends its locked
  block with its inner transaction, so moving the commit from the inner
  block to the lock's exit runs no statement at another time.
- `runoutcome.record_success` (`runoutcome.py:139-153`): its
  `conn.commit()` becomes a `transaction()` around its statement. Spec
  126 rewrites the statement.

**Store errors.** Behavior's table holds on both stores.
`StoreDatabaseError` subclasses `StoreError`, and `StoreIntegrityError`
and `StoreBusyError` subclass `StoreDatabaseError`, all in
`harrier.connection`. The only CLI code that catches `StoreError` is
`harrier store` (`harrier_cli/main.py:1535`), and the API catches none,
so no local command or route prints anything new. The unknown-track
mapping matches the composite foreign key migration 10 adds
(`tracker/schema.py:942`) by its constraint name, read from the migrated
store's catalog in the test.

**The catch sites.** `add_job` (`tracker/store.py:203`) catches
`StoreIntegrityError` and raises `DuplicateJobError`; `add_track`
(`tracks.py:400`) catches it and raises `DuplicateTrackError`;
`logredact.py:105` and `:206` catch `StoreDatabaseError`;
`logsetup.py:140` catches `StoreDatabaseError` and keeps `sqlite3.Error`
and `OSError`, because `harrier.db.connect` raises the driver's own
classes while it opens the file (Scope 3 keeps `db.py` raw). `backup.py`
(`:120`, `:144`), `doctor.py:204` and `db.py:285` work on the SQLite file
itself and keep `sqlite3` classes. A test that catches a `sqlite3` error
class raised through a `Connection` catches the store class instead
(`test_job_events.py` and `test_concurrent_writers.py` among them); a
test whose subject is SQLite itself keeps the driver class. The PR lists
each file.

**The SQLite library check.** `harrier.db.connect` refuses a SQLite
library older than 3.35 with Failure modes' text. Spec 126's `RETURNING
id` needs it.

**What does not change.** The CLI and the API gates
(`harrier_cli/main.py:3091-3094`, `harrier_api/app.py:1370-1371`) and
their text, `POSTGRES_NOT_YET` (`connection.py:41-43`), are unchanged.
Domain SQL is unchanged apart from the two users of `commit()` above.

### Failure modes (amendment of 2026-10-11)

Failure modes above hold for what this spec keeps. In addition:

- **An owner identity that does not hold.** Rollback, `StoreDatabaseError`
  `the run's database identity did not hold`, no domain statement runs.
- **An owner beside SQLite, or not a UUID.** `StoreError` with the texts
  above, before anything opens.
- **An inner transaction fails and the domain catches it.** The inner
  work is undone, the outer transaction continues and commits its own
  work, on both stores.
- **An inner transaction fails and nothing catches it.** The whole
  transaction rolls back, as one block would.
- **A domain function on a Postgres `Connection` before spec 126.** It
  can fail on SQLite-only SQL (`datetime('now')`, `COLLATE NOCASE`,
  `lastrowid`, `ON CONFLICT` targets). Nothing outside the tests opens a
  Postgres `Connection` before spec 127, so nothing reaches one.

### Acceptance criteria (amendment of 2026-10-11)

PR 2, as narrowed:

- [ ] A row reads by position, by name, as values and as a dict on both
      (`services/api/tests/test_seam_on_both_stores.py::test_a_row_reads_by_position_by_name_and_as_a_dict`).
- [ ] A `?` statement binds on both, including one whose literal holds a
      `%`
      (`services/api/tests/test_seam_on_both_stores.py::test_question_marks_bind_on_both_stores`).
- [ ] On both stores, a nested `transaction()` whose error the outer
      block catches leaves the outer block's own writes committed and the
      inner block's undone; on Postgres a statement after the caught
      error runs; an error leaving the outer block rolls back both
      (`services/api/tests/test_seam_on_both_stores.py::test_a_nested_transaction_is_a_savepoint_on_both_stores`).
      This replaces planned test_a_nested_transaction_joins_the_outer_one.
- [ ] On a connection opened with an owner, a statement inside
      `transaction()` and one outside it both run as `harrier_tenant` with
      the owner's `sub`; an insert into an owned table that names no
      `owner_id` stores the owner's id; and with a second owner's row in
      the store, a read returns only the first owner's
      (`services/api/tests/test_seam_on_both_stores.py::test_an_owner_connection_sets_its_identity_in_every_transaction`).
- [ ] When the read-back differs from what the seam set, the transaction
      rolls back with the identity error and the statement that followed
      never runs
      (`services/api/tests/test_seam_on_both_stores.py::test_a_lost_owner_identity_runs_no_statement`).
- [ ] An owner beside SQLite, and an owner that is not a UUID, are
      refused with the texts above and open nothing
      (`services/api/tests/test_seam_on_both_stores.py::test_an_owner_needs_a_postgres_store`).
- [ ] `dialect` names each store, and `busy_timeout_ms` sets SQLite's
      busy timeout and Postgres's `lock_timeout`, 0 sending 1 ms on
      Postgres
      (`services/api/tests/test_seam_on_both_stores.py::test_a_connection_names_its_dialect_and_its_wait`).
- [ ] `Connection` has no `commit` or `rollback`, and `in_transaction` is
      true inside `transaction()` and false outside it, on both
      (`services/api/tests/test_seam_on_both_stores.py::test_a_connection_offers_no_commit_or_rollback`).
- [ ] The three lock tests pass on SQLite unchanged apart from catching
      `StoreBusyError`:
      `services/api/tests/test_job_events.py::test_a_first_move_holds_the_lock_from_read_to_write`,
      `services/api/tests/test_job_events.py::test_backfill_holds_the_lock_from_read_to_write`,
      `services/api/tests/test_job_events.py::test_a_field_update_holds_the_lock_from_read_to_write`.
- [ ] A SQLite library below 3.35 is refused with the text under Failure
      modes
      (`services/api/tests/test_seam_on_both_stores.py::test_an_old_sqlite_library_is_refused`).
- [ ] Every refused probe of spec 103's parity table, run through a
      `Connection` as an owner on Postgres, raises the same class, kind
      and message on both, `unknown track` included
      (`services/api/tests/test_seam_on_both_stores.py::test_every_parity_probe_raises_one_error_on_both_stores`).
- [ ] A Postgres integrity error's message holds no `DETAIL` text
      (`services/api/tests/test_seam_on_both_stores.py::test_a_postgres_refusal_never_quotes_the_row`).
- [ ] A writer held past a short timeout gets `StoreBusyError` on both, and
      a Postgres `busy_timeout_ms` of 0 does not wait forever
      (`services/api/tests/test_seam_on_both_stores.py::test_a_stuck_writer_reports_busy_on_both_stores`).
- [ ] Opening a Postgres store writes no lease and makes no engine query
      (`services/api/tests/test_seam_on_both_stores.py::test_opening_a_postgres_store_takes_no_lease`).
- [ ] On SQLite, the unique-index race in `add_job` is still
      `DuplicateJobError`
      (`services/api/tests/test_tracker_store.py::test_unique_index_race_maps_to_duplicate_error`),
      and a duplicate track slug is `DuplicateTrackError`
      (`services/api/tests/test_seam_on_both_stores.py::test_a_duplicate_slug_is_a_duplicate_track_error`). Spec 126 proves
      both on Postgres.
- [ ] The identity loaders in `logredact.py` return what they can when a
      store read fails, on both
      (`services/api/tests/test_seam_on_both_stores.py::test_identity_reads_survive_a_store_error_on_both_stores`).
- [ ] The PR lists every test file that changed from a `sqlite3` error
      class to a store class, and every caller of the four locked writers
      that wraps one in its own transaction (none is expected).
- [ ] `services/api/tests/test_store_cli.py::test_a_data_command_is_refused_on_a_postgres_url`
      and
      `services/api/tests/test_store_cli.py::test_the_api_refuses_to_start_on_a_postgres_url`
      pass unchanged.
- [ ] `services/api/src/harrier/tracker/schema.py` and
      `packages/contract` do not change.
- [ ] The CI log of `check-python` shows this spec's Postgres tests
      passing, not skipped, and `just check` passes.

Moved out of PR 2's list above, each to the spec that now carries it:
the upsert race, the seven files on both stores, the three lock tests on
Postgres, one job not waiting for another, the timestamp shape, the
folding table, text ordering, an insert returning its id, a duplicate
refused alike through the domain on both, local-only refusals, the docs
and no domain insert supplying an id go to spec 126; the refusal classes
and texts to spec 127; the import-linter contract (both items),
`scrub_secrets` and the concurrent migrate runs to spec 128.

### Honest limitations (amendment of 2026-10-11)

- After this spec, only tests open a Postgres `Connection`.
  `open_store()` has no caller in `services/api/src`: the CLI gate is
  spec 127's, and the API serves Postgres only under spec 104.
- The domain's SQL still speaks SQLite in places until spec 126, so this
  spec's Postgres tests run statements written for the test, not domain
  functions.
- `transaction(immediate=True)` locks nothing on Postgres until spec 126.
  No domain write runs there before then, so nothing relies on it.
- Each Postgres transaction costs one statement more, and on an owner
  connection four more, round trips included. Not measured; the PR
  records `check-python`'s duration before and after.
- `POSTGRES_NOT_YET` still says "until spec 112" after this spec ships.
  Spec 127 replaces the CLI's text and spec 104 the API's.

### Proof / origin (amendment of 2026-10-11)

- `specs/104-the-hosted-api-authenticates-every-request.md`, Behavior
  (the request transaction, steps 2 to 4) and Open decision 3: role and
  claims per transaction, and a savepoint for the domain's own.
- `specs/106-tenant-credentials-and-run-isolation.md`, "The run's database
  session": the claims, the read-back and its text.
- `specs/105-every-personal-table-is-owned-and-policed.md`, the amendment
  of 2026-10-11 (migration 16): `harrier_owner_id()` reads only
  `request.jwt.claims`.
- `specs/115-spec-105-holds-on-a-real-supabase-project.md`, Changes to
  approved specs: this spec's `depends` gains 115.
- `services/api/tests/pg_support.py`, `as_role` and `as_owner`: the
  pattern decision 2 moves into the seam.

## Amendment (2026-10-12): what the seam's pull request settled

Building PR 2 as the amendment of 2026-10-11 narrowed it settled points
that amendment left open, and showed four places where this spec said
more than the stores do. Each is stated here so the code and the spec
agree. No local command or route prints anything new, except the one line
under "The SQLite library check" below.

### Where the spec was wrong

- **A `%` inside a literal is doubled too.** Behavior says a `%` outside a
  literal is doubled. Once parameters are passed, psycopg reads a `%`
  wherever it stands, inside a literal too, and undoubles `%%` there as
  well, so a literal's single `%` failed as a placeholder it cannot read
  (run against Postgres 17, 2026-10-12). The seam always passes
  parameters, an empty sequence included, and doubles every `%`. The
  server receives each literal as written, which is what Failure modes
  promises (`to_psycopg` in `services/api/src/harrier/connection.py`;
  `services/api/tests/test_seam_on_both_stores.py::test_question_marks_bind_on_both_stores`).
- **As an owner, six probes are refused by a privilege, not a trigger.**
  The criterion runs every refused probe of spec 103's parity table as an
  owner. `harrier_tenant` holds no `DELETE` on `tracks` and no `UPDATE`
  or `DELETE` on `job_events` or `job_eligibility` (spec 105), and
  Postgres checks a privilege before any trigger fires. So as the owner
  the probes `job-events-update`, `job-events-delete`,
  `job-events-update-to-status`, `track-delete`,
  `eligibility-update-verdict` and `eligibility-delete` raise
  `StoreDatabaseError`, `permission denied for table <table>`, and no
  trigger text can be compared. The test asserts exactly that, then runs
  those six as the migrating role with the owner's claims, where the
  trigger refuses, and holds them to SQLite's class, kind and message.
  Every other refused probe runs as the owner
  (`services/api/tests/test_seam_on_both_stores.py::test_every_parity_probe_raises_one_error_on_both_stores`).
- **"The same message" is the trigger's.** For `kind="unique"`,
  `"check"`, `"foreign_key"` and `"not_null"` each driver names the
  constraint its own way: SQLite `UNIQUE constraint failed: jobs.url`,
  Postgres `duplicate key value violates unique constraint
  "idx_jobs_url"`. The message is each driver's own text, so a local
  error reads as it did. The test compares class and kind for every
  refused probe, and the message wherever it is a trigger's
  (`kind="refused"`) or the probe declares one, `unknown track` included.
  All of them matched on Postgres 17 on 2026-10-12.
- **Under the row policy Postgres sends no `DETAIL`.** As an owner, a
  refusal's `DETAIL` is withheld by the server, so a test run as the
  owner could not see a leak. The test that proves the seam never quotes
  it runs as the migrating role on a table of its own, where the server
  sends one, and asserts that the `DETAIL` quoted the row and the store
  error does not
  (`services/api/tests/test_seam_on_both_stores.py::test_a_postgres_refusal_never_quotes_the_row`).

### What the implementation settled

- **Names.** `Connection.postgres(driver, *, claims=None)` wraps a psycopg
  connection in autocommit mode. `assume_identity(driver, claims)` is the
  one function decision 2 names. `owner_claims(owner)` builds spec 106's
  claims and refuses an owner that `uuid.UUID` cannot read; the claims
  carry its canonical spelling. `open_store` lives in
  `harrier.connection`. `_write_lock` is gone, and its four users write
  `conn.transaction(immediate=True)`.
- **A deferred SQLite transaction begins where the sqlite3 module begins
  one,** before the first write, as `with conn:` did. An explicit `BEGIN`
  at the start of the block would hold a read snapshot from its first
  read, and a write after another connection's commit would then fail at
  once with `SQLITE_BUSY_SNAPSHOT` instead of waiting. A savepoint opened
  in a block that has not begun yet begins it first, since a savepoint
  that began the transaction would commit when released
  (`services/api/tests/test_seam_on_both_stores.py::test_a_nested_transaction_is_a_savepoint_on_both_stores`).
  `in_transaction` is true from the first line of a block, on both
  stores
  (`services/api/tests/test_seam_on_both_stores.py::test_a_connection_offers_no_commit_or_rollback`).
  A write on SQLite outside any block still opens the sqlite3 module's
  own transaction, and the next outermost block commits it, as `with
  conn:` did.
- **The read-back.** A step the server refuses, such as `SET LOCAL ROLE`
  for a login role that is not a member of `harrier_tenant`, or the
  read-back's own cast of an empty claims setting to `jsonb`, raises the
  same identity error, with the server's as its cause. It reads
  `current_user` and the claims' `sub`, as decision 2 says; spec 104's
  step 3 also reads `harrier_owner_id()` and adds that read itself.
- **How the lost identity is shown.** The test makes the read-back
  differ two ways: a connection whose `BEGIN` never reaches the server,
  as behind a pooler that splits a transaction, so `SET LOCAL` changes
  nothing; and one that sets another owner's claims. Either way the
  caller's insert, inside `transaction()` and outside it, never runs
  (`services/api/tests/test_seam_on_both_stores.py::test_a_lost_owner_identity_runs_no_statement`).
- **Errors.** A driver error raised while fetching rows is mapped too
  (`services/api/tests/test_seam_on_both_stores.py::test_a_failed_fetch_is_a_store_error`),
  and so is one at commit or at close, which no test provokes. A SQLite
  primary key violation is
  `kind="unique"`. A sqlite3 error that carries no code from the
  library, such as a statement on a closed connection, is
  `StoreDatabaseError`, as is an integrity error of any code the table
  does not name. A Postgres error the server did not send, such as a lost
  connection, is named by its class, never by the client's text.
- **A failed block ends quietly.** When rolling back a failed block fails
  too, the caller sees the block's own error.
- **`busy_timeout_ms`** refuses a negative value with `ValueError`.
- **`runoutcome.record_success`** writes in a `transaction()`, and the
  write is seen from another connection
  (`services/api/tests/test_run_outcome.py::test_a_recorded_success_is_committed`).

### The SQLite library check

`harrier.db.connect` raises `StoreLibraryTooOld`, a `StoreError`, before
it takes a lease or creates the file. Failure modes says the CLI prints
it and exits 1. Every command's first open is logging setup's, which
catches the driver's classes, `OSError` and `StoreDatabaseError` only, so
the refusal reached the CLI as a traceback. `harrier_cli.main.main` now
catches `StoreLibraryTooOld`, and nothing else new, and prints `error:
<text>` with exit 1
(`services/api/tests/test_seam_on_both_stores.py::test_an_old_sqlite_library_is_refused`).

### What the pull request lists

- **Test files that caught or raised a `sqlite3` error class through a
  `Connection` and now name a store class:** `test_cli_connections.py`,
  `test_concurrent_writers.py`, `test_eligibility.py`,
  `test_job_events.py` (the three lock tests among them),
  `test_logging.py` (a stand-in for `identity_values` now raises what the
  real one can), `test_resume_documents.py`, `test_track_framing.py` and
  `test_tracks_cli.py`, all under `services/api/tests`. Tests whose
  subject is SQLite itself keep the driver's classes:
  `test_migration_runner.py`, `test_tracks.py`, `test_dialect_parity.py`,
  and the migration case of `test_track_framing.py`.
- **Test files whose `commit()` or `rollback()` became `transaction()`:**
  `test_academic_discovery.py`, `test_api_tracks.py`, `test_backup.py`,
  `test_concurrent_writers.py` (its subprocess writer too),
  `test_database_ownership.py`, `test_eligibility.py`,
  `test_job_events.py`, `test_logging.py`, `test_resume_documents.py`,
  `test_scoring_model.py`, `test_track_framing.py`,
  `test_track_isolation.py`, `test_tracker_invariants.py`,
  `test_tracks.py`, `test_ui_operations.py` and `test_userconfig.py`.
  `test_backup.py` now leaves its write outside any block, so nothing
  commits it, and closes the connection, which discards it.
- **Callers of the four locked writers that wrap one in their own
  transaction:** none. No `transaction()` block under
  `services/api/src` calls `set_status`, `update_fields`,
  `backfill_events` or `record_eligibility`.

### The store password in logs and HTTP errors

The criterion recorded from spec 128 (planned
test_no_log_or_http_error_carries_the_store_password) sits in PR 2's
first list, which the amendment of 2026-10-11 replaced, and neither the
narrowed list nor "Where each item went" carries it. This pull request
does not build it. Its two sinks are reached only once an error raised on
a Postgres store reaches a log record or a route: the API serves Postgres
only under spec 104, and a command other than `harrier store` runs there
only under spec 127. The seam narrows the gap. A store error raised
through a Postgres `Connection` carries the server's primary message or
the driver error's class, never the client's text, so its own message
quotes no URL. The driver's error stays chained as `__cause__`, as
Behavior requires, and a logged traceback prints it. Akin places the
criterion; spec 104 (HTTP detail) and spec 127 (the log stream of a
command run on Postgres) are the candidates.

### Honest limitations (amendment of 2026-10-12)

- The parity test creates and migrates a Postgres database per refused
  probe, and two for each of the six above, because the probes name job
  1 and ids are numbered across owners. Spec 126's template database
  would make that cheaper.
- `check-python`'s duration in CI was not measured here; the pull
  request records it.
