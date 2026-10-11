---
spec: 128
title: Three review findings on the Postgres store
status: proposed
approved: no
milestone: M10
depends: [035, 090, 103]
---

# Spec 128: Three review findings on the Postgres store

Split from spec 112 on 2026-10-11 (spec 112, Amendment (2026-10-11):
split, owner and nesting, decision 1). This is part D: the three findings
of the post-merge review of PRs #194 and #207 (2026-10-10) that spec 112
carried as Scope items 11, 14 and 15. None of them needs the domain on
Postgres, so this spec is independent of spec 112's PR 2, of specs 126
and 127, and of spec 115's acceptance, and can land first. Line numbers are from
`main` at `87c3926` on 2026-10-11.

## Problem

1. **Nothing confines psycopg.** Three modules under `services/api/src`
   import it today: `harrier.pgstore` (`pgstore.py:42`, `:102`, `:112`,
   `:174`), `harrier.connection`, lazily inside functions
   (`connection.py:311`, `:333`), and `harrier_cli.main`, which imports it
   in `_store_postgres` (`harrier_cli/main.py:1555`) to catch
   `psycopg.Error` at `:1560` and `:1574`. Spec 112 meant the driver to
   have two importers, the seam and the driver module, so that a domain
   module never reaches the driver past the seam. No import-linter
   contract says so (`services/api/pyproject.toml:88-132` holds four
   others), so a fourth importer would pass `just check`. (Data integrity
   6.)
2. **`scrub_secrets` leaves a Postgres URL's password.**
   `services/api/src/harrier/sources/__init__.py:27-43` scrubs `password=`
   query parameters and Telegram bot paths, not the password in a URL's
   userinfo. Run against synthetic URLs on 2026-10-11,
   `scrub_secrets("postgresql://harrier:s3cret@db.example.test:5432/tracker")`
   returns the text unchanged, as does the `postgres://` form; only
   `?password=s3cret` became `?password=REDACTED`. `scrub_secrets` guards
   run summaries, events and the exception boundary (spec 035). Once runs
   open a Postgres store, a driver message that quotes the URL would reach
   them unscrubbed. `pgstore`'s own connection error scrubs its message
   already (`pgstore.py:109-145`); nothing else does. (Privacy 3.)
3. **`harrier store migrate` reports a version it did not find.**
   `apply_postgres_migrations` reads `before` at `pgstore.py:178`, before
   it takes the advisory lock at `:183`. Two runs started together on an
   empty store both read version 0, and both print `postgres 0 -> <target>`
   (`harrier_cli/main.py:1569`), though the lock makes each migration
   apply once and the second run applied none. (Data integrity 4.)

## Scope

1. **An import-linter contract** in `services/api/pyproject.toml` forbids
   `psycopg` to `harrier`, `harrier_api` and `harrier_cli`, except the
   imports `harrier.pgstore -> psycopg` and `harrier.connection ->
   psycopg`.
2. **The CLI stops importing psycopg.** `harrier.pgstore` raises a new
   `StoreServerError(StoreError)`, defined in `harrier.connection`, where
   the CLI caught `psycopg.Error` (Behavior). The CLI catches that class.
   What `harrier store` prints does not change.
3. **`scrub_secrets` replaces a Postgres URL's password** (Behavior).
4. **The migration runner reads `before` under a session-level lock**
   (Behavior).

## Behavior

**psycopg has two importers.** `lint-imports`, which `just check` runs
(`justfile:75`), fails when any module under the three root packages
other than `harrier.pgstore` and `harrier.connection` imports `psycopg`,
at module level or inside a function. The contract:

| Field | Value |
|---|---|
| name | `psycopg is reached only through the store seam` |
| type | `forbidden` |
| source_modules | `harrier`, `harrier_api`, `harrier_cli` |
| forbidden_modules | `psycopg` |
| ignore_imports | `harrier.pgstore -> psycopg`, `harrier.connection -> psycopg` |

`include_external_packages` is already true (`pyproject.toml:91`).

**`harrier store` prints what it printed.** `migrate_postgres` and a new
`pgstore` function that reads a store's version by URL catch
`psycopg.Error` after connecting and raise `StoreServerError` whose
message is `server_reason(error, url)`, the one line the CLI prints today
(`pgstore.py:81-97`). They raise it after the `except` block, so the
driver's error is not chained as `__context__`, the rule `postgres_connect`
follows (`pgstore.py:141-145`). The CLI prints `error: a migration failed:
<message>` and `error: reading the store's version failed: <message>`, as
today. `services/api/tests/test_store_cli.py::test_a_failed_migration_exits_1_with_the_drivers_message`,
`services/api/tests/test_store_cli.py::test_store_status_fails_with_one_line_after_connecting`
and
`services/api/tests/test_store_cli.py::test_the_servers_message_after_connecting_is_scrubbed`
pass unchanged.

**A Postgres URL's password is scrubbed.** `scrub_secrets` replaces the
password in the userinfo of a `postgres://` or `postgresql://` URL, the
scheme in any case, with `REDACTED`, and keeps the scheme, user, host,
port, database and query.

| Text | After `scrub_secrets` |
|---|---|
| `postgresql://harrier:s3cret@db.example.test:5432/tracker` | `postgresql://harrier:REDACTED@db.example.test:5432/tracker` |
| `postgres://harrier:s3cret@db.example.test/tracker` | `postgres://harrier:REDACTED@db.example.test/tracker` |
| `postgresql://harrier@db.example.test/tracker` | unchanged |
| `postgresql://db.example.test/tracker?password=s3cret` | `postgresql://db.example.test/tracker?password=REDACTED`, as today |

The password runs from the `:` after the user to the last `@` before the
next `/`, `?`, `#` or whitespace. A password holding a raw `@` is
replaced whole, at the cost of hiding the part of the host before its own
`@`; a percent-encoded password is replaced as written.

**`harrier store migrate` reports what it found under the lock.** The
runner takes a session-level advisory lock on `MIGRATION_LOCK_KEY`
(`pgstore.py:48`) before it reads `before`, and holds it until its last
migration commits, then releases it. Each migration still runs in its own
transaction and still takes the transaction-level lock on the same key
(`:187`), whole or not at all (spec 090); a session that holds an
advisory lock is granted it again at either level. A second run started
with the first waits for the session lock, then finds the store at the
target, applies nothing and prints `postgres <target> -> <target>`.

## Failure modes

- **A module other than `harrier.pgstore` or `harrier.connection` imports
  `psycopg`.** `lint-imports` fails, and so does `just check`.
- **A driver or library message quotes a Postgres URL.** The password is
  `REDACTED` wherever `scrub_secrets` runs; the host and database stay,
  so the message still says which store failed.
- **Two `harrier store migrate` runs at once.** The second waits for the
  first's session lock, applies nothing, and reports the version it found
  under the lock. Neither reports a migration it did not apply.
- **A run killed while holding the session lock.** Postgres releases a
  session lock when the session ends, so the next run proceeds.
- **A run that hangs while holding the lock.** The second run waits
  without a limit, as it waits on the transaction lock today. No
  `lock_timeout` is set here.

## Acceptance criteria

- [ ] The contract above is in `services/api/pyproject.toml`, and
      `lint-imports` in `just check` passes with it. The PR records
      `lint-imports` failing with a third importer added.
- [ ] No module under `services/api/src` other than `harrier/pgstore.py`
      and `harrier/connection.py` imports `psycopg`, and the three
      `test_store_cli.py` tests named under Behavior pass unchanged.
- [ ] `scrub_secrets` gives each row of the table above (planned
      test_scrub_secrets_hides_a_postgres_url_password).
- [ ] Two `harrier store migrate` runs started together on an empty
      Postgres store apply each migration once; one reports
      `postgres 0 -> <target>` and the other `postgres <target> -> <target>`
      (planned test_concurrent_migrate_runs_report_what_each_found).
- [ ] `services/api/tests/test_store_cli.py::test_store_migrate_prints_before_and_after_on_postgres`
      passes unchanged.
- [ ] `services/api/src/harrier/tracker/schema.py` and
      `packages/contract` do not change.
- [ ] `just check` passes, and the CI log of `check-python` shows the
      Postgres tests above passing, not skipped.

## Honest limitations

- A session-level advisory lock holds only on a connection that keeps
  its session. Behind a transaction-mode pooler it does not. Spec 110
  (Open decision 8) routes migrations through Supabase's session pooler,
  which keeps it.
- The scrub knows the URL shapes above. A driver message that quotes a
  password outside a URL, as a bare parameter, is caught only by the
  `password=` rule that exists today, and by `pgstore`'s own scrub of its
  connection errors.
- The contract proves which modules import psycopg, not what they do with
  it.

## Migration

None. No schema change, no data change, no contract change. `harrier
store` prints what it printed.

## Options weighed

- **Allow `harrier_cli.main -> psycopg` in the contract.** Smaller, and
  leaves the CLI tied to the driver's error classes, which is what spec
  112's seam exists to end. Not chosen.
- **Scrub the URL at every call site that might quote it.** The call
  sites that leak are the ones nobody thought of (`sources/__init__.py:34-40`).
  Not chosen: the boundary scrubs.
- **Read `before` inside the first transaction, under the transaction
  lock.** The lock ends at that transaction's commit, before the
  migrations run, so a second run could still read 0 between them. Not
  chosen.

## Open decisions for Akin

1. **The new class.** Recommended: `StoreServerError`, for a statement the
   server refused on the driver connection `harrier store` uses. Spec
   112's `StoreDatabaseError` covers errors raised through a `Connection`,
   which `harrier store` does not use. Alternative: define
   `StoreDatabaseError` here and let spec 112 extend it, which ties this
   spec's landing to spec 112's error table.

## Proof / origin

- `specs/112-the-tracker-domain-runs-on-either-store.md`: Scope items 11,
  14 and 15, Behavior ("A Postgres URL's password is scrubbed",
  "`harrier store migrate` reports what it found under the lock"), and
  the Amendment (2026-10-11): split, owner and nesting.
- The post-merge review of PRs #194 and #207 (2026-10-10): data integrity
  4 and 6, privacy 3, as spec 112 recorded them.
- `specs/035-local-api-exposure.md`: `scrub_secrets` at the boundary.
- `specs/090-migrations-apply-whole-or-not-at-all.md`: one transaction per
  migration.
- `specs/103-the-tracker-store-speaks-postgres.md`: `harrier store` and
  its texts.
- `specs/110-deploy-pipeline-and-environments.md`, Open decision 8: the
  session pooler for migrations.

## Out of scope

- The seam, the domain on Postgres, and the CLI's refusal classes: specs
  112, 126 and 127.
- Connection pooling and the pooler's modes: spec 110.
- A timeout on the migration lock.
