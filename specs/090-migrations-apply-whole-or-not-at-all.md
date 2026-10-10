---
spec: 090
title: A migration applies whole or not at all
status: shipped
approved: yes
milestone: M9
depends: [041, 060, 061, 079]
---

# Spec 090: A migration applies whole or not at all

## Problem

The migration runner in `services/api/src/harrier/db.py` (`_apply_schema`,
lines 258 to 269 at commit cd5665d) applies each pending migration inside a
`with conn:` block. That block is the `sqlite3` module's transaction
context, and the module opens its implicit transaction only before a data
statement (INSERT, UPDATE, DELETE, REPLACE). A `CREATE TABLE`, `ALTER TABLE`
or `CREATE TRIGGER` runs in autocommit mode and is on disk the moment it
returns. SQLite itself can roll back schema changes; the Python wrapper never
asks it to.

So a migration is not one unit. If its third statement fails, its first two
statements stay, its `schema_version` row is never written, and the next
open retries the whole migration from the first statement, which now fails
on "table already exists" or "duplicate column name". The database is
stuck between two versions with nothing that can move it either way except
a person with `sqlite3`.

Nothing has gone wrong this way yet, because migrations one to seven were
small. Spec 091's migration adds a table, a column, triggers and an index in
one step, and spec 093's adds a column with a CHECK. A failure between any
two of those statements is exactly the partial schema above. The runner has
to be fixed before those migrations exist, not after one of them fails.

A second gap sits beside the first. Two processes opening a new database
file at the same moment both read `MAX(version)` as empty and both start
migration one. There is no lock between the read and the write, so the
second process fails on the first process's `CREATE TABLE`. Spec 061 made
the one-kernel rule, and spec 051 proved two writers under one kernel, but
neither covers the first open: the API container and a host command, or two
tests sharing a temporary directory, can race it today.

## Scope

- `services/api/src/harrier/db.py`: `_apply_schema`, and the `connect`
  sequence around it (the connection is closed when a migration fails).
- `services/api/tests/test_migration_runner.py` (new): the tests below.
- Nothing else. No new migration, no change to any existing migration's
  statements, no ADR, no schema change, no contract change, no change under
  `apps/web`. `services/api/src/harrier/tracker/schema.py` is not touched.

## Behavior

### One transaction per migration

`_apply_schema` reads `MAX(version)` once. If nothing is pending it returns
without opening a transaction, so an ordinary open is unchanged.

If something is pending, the migration phase runs with the connection's
`isolation_level` set to `None`, so the `sqlite3` module issues no `BEGIN`
or `COMMIT` of its own. For each pending migration, in version order:

1. `BEGIN IMMEDIATE`. This takes the write lock before anything is read,
   and waits up to `BUSY_TIMEOUT_MS` for another writer.
2. Re-read `MAX(version)` under the lock. If this migration's version is
   now at or below it, another process applied it in between: `ROLLBACK`
   (nothing was written) and continue with the next version.
3. Execute the migration's statements, then insert its `schema_version`
   row, all inside the same transaction.
4. `COMMIT`.

On any exception in step 3 or 4: `ROLLBACK`, then re-raise the original
exception unchanged (its type and message are what the caller sees today).

After the phase, whether it succeeded or raised, `isolation_level` is
restored to the value the connection had, so a `with conn:` block in the
store behaves exactly as it does now.

### A failed migration leaves nothing behind

After a failed migration, `schema_version` reads the version it read before,
no object the migration created exists, and no column it added exists. The
next open retries the same migration from its first statement.

`connect` closes the connection before the exception leaves it. A caller
that never received the connection cannot close it (spec 076), and an open
handle on a failed open would hold the file and its `-wal` and `-shm`
companions for the life of the process.

### Two first opens apply each migration once

Two processes opening the same new file at once both succeed. One applies
each migration; the other waits on `BEGIN IMMEDIATE`, re-reads the version
under the lock, finds it applied, and skips it. `schema_version` holds each
version once and the schema matches a single open.

### The container's SQLite

Transactional DDL and `BEGIN IMMEDIATE` are old SQLite features, older than
any library the project can be running. The host's version is whatever the
operator's Python links; the container's is the Debian library of the
`python:3.12-slim-bookworm` image (`Dockerfile`). Neither is assumed. The
implementing pull request records the version the container reports from
`docker exec harrier python -c "import sqlite3; print(sqlite3.sqlite_version)"`
and the host's from the same one-liner, and the partial-schema test
(`services/api/tests/test_migration_runner.py::test_a_failed_migration_leaves_no_partial_schema`)
is mirrored inside the container by the script the amendment describes.

## Failure modes

- **A statement fails mid-migration** (a typo in a future migration, a CHECK
  an existing row violates). Rolled back; `schema_version` unchanged; the
  exception propagates; the connection is closed.
- **Another writer holds the lock past the busy timeout.** `BEGIN
  IMMEDIATE` raises `sqlite3.OperationalError` ("database is locked").
  Nothing was written. The caller sees the same error a locked write raises
  today.
- **The process dies mid-migration.** SQLite's journal discards the
  uncommitted transaction at the next open. This is SQLite's guarantee for
  any transaction and is not tested here.
- **A migration needs `PRAGMA foreign_keys` off.** That pragma is a no-op
  inside a transaction, so a migration that must rebuild a table other rows
  reference cannot run under this runner. No such migration exists; spec
  091 is written so that none is needed (it never rebuilds `jobs`). A future
  one needs a spec that amends this runner first.
- **`isolation_level` left at `None` after an error.** The restore runs in
  a `finally`, so a caller never receives a connection in a different
  transaction mode from today's.

Must not introduce: a migration applied twice; a `schema_version` row
written without its statements; a connection returned in a different
isolation level from today's; a new migration or a change to an existing
one; a test that opens the operator's data directory (spec 060); a second
place that applies migrations.

## Acceptance criteria

Tests in `services/api/tests/test_migration_runner.py` unless named
otherwise. Every database is built under `tmp_path` with synthetic rows.

- [x] With a migration list whose second statement is invalid, `connect`
      raises, the table the first statement created does not exist, and
      `schema_version` is unchanged. Run first against the runner as it is
      at cd5665d and seen to fail there; the pull request records both runs
      (`services/api/tests/test_migration_runner.py::test_a_failed_migration_leaves_no_partial_schema`)
- [x] The connection is closed before the error leaves `connect`, proven by
      a test that fails if a connection to the file is still open
      (`services/api/tests/test_migration_runner.py::test_a_failed_migration_closes_the_connection`)
- [x] Two processes opening the same new file at once both succeed, each
      version appears once in `schema_version`, and the schema equals a
      single open's
      (`services/api/tests/test_migration_runner.py::test_two_first_opens_apply_each_migration_once`)
- [x] After `connect` returns, the connection's `isolation_level` is the
      module default and a `with conn:` block still commits, on the success
      path and after a failed migration
      (`services/api/tests/test_migration_runner.py::test_connect_returns_a_connection_in_its_usual_transaction_mode`)
- [x] An open with nothing pending opens no transaction
      (`services/api/tests/test_migration_runner.py::test_an_open_with_nothing_pending_takes_no_lock`)
- [x] `services/api/tests/test_userconfig.py::test_every_migration_version_is_unique_and_ordered`
      and
      `services/api/tests/test_scoring.py::test_a_migrated_database_matches_a_fresh_one`
      pass unchanged
- [x] The tests in `services/api/tests/test_concurrent_writers.py` pass
      unchanged
- [x] The pull request records the SQLite version reported inside the
      container and on the host, and the result of the in-container script
      described in the amendment (the test itself cannot run there)
- [x] Each test above fails with its behavior removed, checked by removing
      each behavior in turn and recorded in the pull request
- [x] All gates green on the pull request

## Honest limitations

- **SQLite only.** The transaction semantics here are SQLite's. A hosted
  store (ADR-012, and the ADR-013 it hands off to) has its own migration
  story and this runner is not it.
- **`PRAGMA foreign_keys` cannot change inside a transaction**, so a
  table rebuild under foreign keys is outside what this runner can do. That
  is a constraint on every future migration, and spec 091 is the first to
  live under it.
- **The kill-mid-migration case is SQLite's guarantee, not a test.**
- **The two-process test runs under one kernel**, which is the supported
  case (ADR-011). Nothing here is proven across the Docker bind mount, and
  nothing needs to be: one kernel owns the file.
- **A database newer than the code** (a `schema_version` above the last
  migration the code knows) is opened without complaint, as today. See
  Open decisions.

## Migration

None for the operator. A database at version seven is opened, nothing is
pending, and no transaction is taken. The container does not need a
rebuild for its data; it needs one to carry the new runner before specs 091
and 093 land, which `just container-up` does.

## Options weighed

- **Leave the runner and keep migrations small.** Rejected: migration eight
  cannot be one statement, and "small" is not a property a test can hold.
- **One transaction around every pending migration.** Rejected: a failure
  in the second pending migration would undo a good first one, and the lock
  would be held for the whole phase. Per-migration is the unit a version
  number describes.
- **One transaction per migration, lock first** (chosen).
- **An external migration tool.** Rejected: a dependency and a second
  schema language for seven migrations in one file.

## Open decisions for Akin

1. Whether `connect` should refuse a database whose `schema_version` is
   above the last migration the code knows. Recommendation: not in this
   spec. It is a real gap (an old container opening a database a newer
   host migrated), it is independent of atomicity, and it deserves its own
   acceptance criterion and error message.
2. Whether to switch the transaction mode with `isolation_level = None` or
   with Python 3.12's `autocommit` attribute. Recommendation:
   `isolation_level`, because every other transaction in the service is
   written against it and the two must not mix on one connection.

## Proof / origin

- The runner: `services/api/src/harrier/db.py`, `_apply_schema` (lines 258
  to 269 at cd5665d); `PRAGMA foreign_keys=ON` at line 247.
- The Python behavior: the `sqlite3` module's "Transaction control"
  documentation, which states that DDL is not wrapped in an implicit
  transaction.
- SQLite's own: `BEGIN IMMEDIATE` and transactional schema changes
  (https://sqlite.org/lang_transaction.html).
- The skip rule the race abuses: spec 041, "Found while implementing" (a
  version at or below the recorded one is skipped), and spec 079's
  amendment on migration numbering.
- One kernel, one busy timeout: spec 061, ADR-011, spec 051's
  `services/api/tests/test_concurrent_writers.py`.
- Connections are closed by their opener: spec 076. Tests never touch the
  operator's data: spec 060.

## Out of scope

- Any new migration (spec 091, spec 093).
- Refusing a database newer than the code (Open decisions, item 1).
- Migrations that must turn foreign keys off.
- Migration tooling for a hosted store (ADR-013).

## Amendment (2026-10-08, during implementation)

What the two-process criterion found, each with the test that proves it:

- **The busy timeout is set before the journal mode.** The second process
  reached `PRAGMA journal_mode=WAL` while the first still held the write
  lock for migration one, and with no timeout yet set it failed there with
  "database is locked" instead of waiting. The pragma order in `connect`
  changes; nothing else about the connection does.
  `tests/test_migration_runner.py::test_two_first_opens_apply_each_migration_once`.
- **The journal-mode pragma is retried within the same bound.** With the
  timeout in place the second process still failed at that pragma: SQLite
  answers it with busy without consulting the busy handler while another
  connection is mid-write. `connect` now retries the pragma, sleeping
  briefly, until it succeeds or `BUSY_TIMEOUT_MS` has passed, and re-raises
  any other error at once. Same test.
- **The in-container check is a script, not the test.** The image installs
  no test dependencies (`Dockerfile`, `--no-dev`), so the partial-schema
  test cannot run inside it. What was run there instead, and is recorded in
  the pull request with both SQLite versions, is a script against a
  temporary file in the container that issues the same statements through
  the container's own `sqlite3` module (`BEGIN IMMEDIATE`, a `CREATE
  TABLE`, a statement that cannot run, `ROLLBACK`) and prints the tables
  left behind: none. The criterion is amended to say so.
- **"Two processes" is two processes.** The test starts two interpreters
  that each call `connect` on the same new file, with every `CREATE TABLE`
  slowed and a pause after every `COMMIT` in both, so each process arrives
  while the other holds the lock and each gets the lock between the
  other's migrations. Each therefore reads a version the other has moved
  on from and must find that work under the lock rather than redo it.
  Under the runner at cd5665d the second died on "database is locked";
  under this runner both succeed and the schema equals a single open's.
- **One mutant survives, by design.** With the explicit `ROLLBACK` on
  failure removed, every test still passes, because `connect` closes the
  connection and closing discards the open transaction. The rollback stays
  as the backstop for any later caller of `_apply_schema` that does not
  close, the way spec 061 keeps `create_backup`'s own check.
