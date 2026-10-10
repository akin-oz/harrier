---
spec: 112
title: The tracker domain runs on either store
status: proposed
approved: no
milestone: M10
depends: [076, 079, 090, 092, 103, 105]
---

# Spec 112: The tracker domain runs on either store

## Problem

Spec 103 creates a Postgres store with the tracker's schema, and nothing
uses it. Every domain read and write is written for `sqlite3`. Line numbers
below are from the spec 103 branch on 2026-10-10; counts say how they were
taken.

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
`harrier_api/app.py:161` (the health count) and `harrier_cli/main.py:1426-1433`
(`harrier store status` reading a SQLite file read-only).

**What does not run on Postgres as written:**

- `?` placeholders in every statement. psycopg 3 takes `%s`.
- `with conn:` as a transaction: 13 sites in 5 modules (`tracker/store.py`
  7, `tracks.py` 2, `userconfig/store.py` 2, `profile/store.py` 1,
  `tracker/migrate_legacy.py` 1). On a `sqlite3.Connection` it commits or
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
  (`tracker/schema.py:427-432`) as `psycopg.errors.RaiseException`, which
  is not an integrity error there. An unknown track is a trigger message in
  SQLite (`tracker/schema.py:343-354`) and a foreign key violation in
  Postgres (`tracker/schema.py:464`).
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
refuses a Postgres URL (`harrier_cli/main.py:2865-2868`) and so does the
API (`harrier_api/app.py:931-932`), both with `POSTGRES_NOT_YET`
(`pgstore.py:43-45`), which says "until spec 112".

## Scope

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
    `harrier.pgstore` and `harrier.connection`.
12. **Docs.** `docs/architecture.md`'s hosted section says the domain runs
    on either store, names `harrier/connection.py`, and stops saying "until
    spec 112". `specs/103-the-tracker-store-speaks-postgres.md` is not
    edited.

**Delivery.** One spec, two pull requests in order (Open decision 9).
PR 1 is a refactor with no change in behavior: items 1, 2 (SQLite only), 3,
and `with conn:` replaced by `transaction()`. No SQL text and no exception
class changes in it; the suite passes with only annotation and fixture-type
edits. PR 2 is the behavior: items 2 (Postgres), 4 to 12.

## Behavior

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

## Acceptance criteria

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
      (planned test_a_row_reads_by_position_by_name_and_as_a_dict).
- [ ] A `?` statement binds on both, including one whose literal holds a
      `%` (planned test_question_marks_bind_on_both_stores).
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
      modes (planned test_an_old_sqlite_library_is_refused).
- [ ] Every refused probe of spec 103's parity table raises the same class,
      kind and message on both, `unknown track` included (planned
      test_every_parity_probe_raises_one_error_on_both_stores).
- [ ] A duplicate track slug is `DuplicateTrackError`, and a unique-index
      race in `add_job` is `DuplicateJobError`, on both (planned
      test_a_duplicate_is_refused_alike_on_both_stores).
- [ ] A Postgres integrity error's message holds no `DETAIL` text (planned
      test_a_postgres_refusal_never_quotes_the_row).
- [ ] A writer held past a short timeout gets `StoreBusyError` on both, and
      a Postgres `busy_timeout_ms` of 0 does not wait forever (planned
      test_a_stuck_writer_reports_busy_on_both_stores).
- [ ] Opening a Postgres store writes no lease and makes no engine query
      (planned test_opening_a_postgres_store_takes_no_lease).
- [ ] `cutover` and `migrate_legacy.migrate` refuse a Postgres connection
      with the texts under Behavior (planned
      test_local_only_work_refuses_a_postgres_connection).
- [ ] The identity loaders in `logredact.py` return what they can when a
      store read fails, on both (planned
      test_identity_reads_survive_a_store_error_on_both_stores).
- [ ] Every top-level command has one refusal class, and the two texts
      and the API's text are as under Behavior (planned
      test_every_command_has_a_postgres_refusal_class). Spec 103's
      refusal tests in `services/api/tests/test_store_cli.py` are updated
      to the new texts.
- [ ] The import-linter contract fails when a third module imports
      `psycopg` (recorded in the PR).
- [ ] `services/api/src/harrier/tracker/schema.py` and
      `packages/contract` do not change.
- [ ] `docs/architecture.md` describes the domain on either store and no
      longer says "until spec 112".
- [ ] `just check` passes.

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

- Authentication, the `authenticated` role, and the API on Postgres:
  spec 104.
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
