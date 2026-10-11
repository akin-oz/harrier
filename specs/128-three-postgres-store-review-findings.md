---
spec: 128
title: Three review findings on the Postgres store
status: accepted
approved: yes
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
or any module imports `psycopg_binary`, with an `import` or `from`
statement, at module level or inside a function. It reads statements, so
an import made at run time is not seen (Honest limitations). The
contract:

| Field | Value |
|---|---|
| name | `psycopg is reached only through the store seam` |
| type | `forbidden` |
| source_modules | `harrier`, `harrier_api`, `harrier_cli` |
| forbidden_modules | `psycopg`, `psycopg_binary` |
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
| `postgresql://harrier@db.example.test/tracker?sslpassword=s3cret` | `postgresql://harrier@db.example.test/tracker?sslpassword=REDACTED` |
| `host=db.example.test password = 's3 cret' dbname=tracker` | `host=db.example.test password = REDACTED dbname=tracker` |
| `postgresql+psycopg://harrier:s3cret@db.example.test/tracker` | `postgresql+psycopg://harrier:REDACTED@db.example.test/tracker` |
| `postgresql:\/\/harrier:s3cret@db.example.test\/tracker` | `postgresql:\/\/harrier:REDACTED@db.example.test\/tracker` |
| `DSN_postgresql://harrier:s3cret@db.example.test/tracker` | `DSN_postgresql://harrier:REDACTED@db.example.test/tracker` |

The password runs from the `:` after the user to the last `@` before the
next `/`, `?`, `#` or whitespace. A password holding a raw `@` is
replaced whole, at the cost of hiding the part of the host before its own
`@`; a percent-encoded password is replaced as written. The scheme may
carry a driver after a `+`, its slashes may be JSON-escaped as `\/`, and
it may follow a word character with no space between.

The value of every parameter whose name holds `pass` or `secret`, in any
case, is `REDACTED`: in a query and in libpq's keyword/value form, with
or without spaces around the `=`, quoted or not, and a quoted value cut
short before its closing quote to the end of its line. It is the rule
`harrier.connection` uses for the parameters it scrubs. The value of any
other parameter is kept, and read for secrets of its own.

**Every URL `store_target` accepts has its password scrubbed whole.**
`store_target` refuses a URL holding a raw whitespace character of any
kind, or a raw character of Unicode category C (control, format,
surrogate, private use, unassigned), with the fixed text that tells the
user to percent-encode. Before, it refused only a raw tab, CR and LF of
these, and a password holding a raw no-break space, vertical tab or
ideographic space was accepted, and left whole by the scrub, which ends a
password at whitespace. A raw letter or symbol, such as `é` or `€`, is
still accepted. This tightens spec 103's refusal of raw delimiters.

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
- **The session ends before the unlock.** psycopg learns of it only on
  the next statement, so the unlock raises. That error is dropped: the
  runner's own result or error is what the caller sees, and the lock went
  with the session.

## Acceptance criteria

- [ ] The contract above is in `services/api/pyproject.toml`, and
      `lint-imports` in `just check` passes with it. The PR records
      `lint-imports` failing with a third importer added.
- [ ] No module under `services/api/src` other than `harrier/pgstore.py`
      and `harrier/connection.py` imports `psycopg`, and the three
      `test_store_cli.py` tests named under Behavior pass unchanged.
- [ ] `scrub_secrets` gives each row of the table above
      (`services/api/tests/test_api_exposure.py::test_scrub_secrets_hides_a_postgres_url_password`).
- [ ] Two `harrier store migrate` runs started together on an empty
      Postgres store apply each migration once; one reports
      `postgres 0 -> <target>` and the other `postgres <target> -> <target>`
      (`services/api/tests/test_store_cli.py::test_concurrent_migrate_runs_report_what_each_found`).
- [ ] `services/api/tests/test_store_cli.py::test_store_migrate_prints_before_and_after_on_postgres`
      passes unchanged.
- [ ] `services/api/src/harrier/tracker/schema.py` and
      `packages/contract` do not change.
- [ ] Every character a password could hold raw is refused by
      `store_target` or masked by `scrub_secrets`
      (`services/api/tests/test_postgres_store.py::test_every_url_store_target_accepts_has_its_password_scrubbed`).
- [ ] A parameter named as a secret is masked in a query and in the
      keyword/value form
      (`services/api/tests/test_api_exposure.py::test_every_secret_named_parameter_is_scrubbed`),
      and other parameters are kept
      (`services/api/tests/test_api_exposure.py::test_scrubbing_leaves_ordinary_parameters_alone`).
- [ ] The table's driver, JSON and glued rows hold
      (`services/api/tests/test_api_exposure.py::test_a_postgres_url_in_another_dress_is_scrubbed`).
- [ ] A session ended before the unlock leaves the runner's result
      (`services/api/tests/test_postgres_store.py::test_a_session_lost_after_the_last_commit_still_reports_the_migration`)
      and its refusal
      (`services/api/tests/test_postgres_store.py::test_a_refusal_is_reported_when_the_session_is_lost_before_the_unlock`)
      as they were.
- [ ] `just check` passes, and the CI log of `check-python` shows the
      Postgres tests above passing, not skipped.

## Honest limitations

- A session-level advisory lock holds only on a connection that keeps
  its session. Behind a transaction-mode pooler it does not. Spec 110
  (Open decision 8) routes migrations through Supabase's session pooler,
  which keeps it.
- The scrub knows the shapes above. `store_target` refuses each of these,
  but a message that carried one would leave the password: a URL
  percent-encoded inside another URL (`?next=postgresql%3A%2F%2F...`),
  text cut off before the password's `@`, a raw `/`, `?` or `#` in the
  password, and a raw `@` in the user. `pgstore`'s own scrub of its
  connection errors does not depend on these shapes: it learns the
  password from the URL itself.
- A secret parameter in a JSON object (`"password": "..."`) is not
  masked: the rule reads `=`, not `:`.
- The contract proves which modules import psycopg, not what they do with
  it. It reads `import` and `from` statements only, so it does not see
  `importlib.import_module("psycopg")`, `__import__`, a module taken from
  `sys.modules`, or a psycopg name that `harrier.pgstore` re-exports to
  another module. None of these exists today; review is the guard.

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

## Amendment (2026-10-11): what the implementation settled

Building this spec settled five points it left implicit. Each is stated
here so the code and the spec agree. None changes what `harrier store`
prints.

- **The new `pgstore` function is `read_postgres_version(url)`.** It and
  `migrate_postgres` raise `StoreServerError` after their `except` block.
  `services/api/tests/test_store_cli.py::test_a_driver_error_after_connecting_is_neither_chained_nor_context`
  holds both to it, with the driver's error neither `__cause__` nor
  `__context__`.
- **Two tests named the driver's class and now name `StoreServerError`.**
  `services/api/tests/test_migration_runner.py::test_a_failed_migration_leaves_no_partial_schema_on_postgres`
  expected `psycopg.Error` from `migrate_postgres`, and
  `services/api/tests/test_owner_policy.py::test_a_failed_migration_leaves_no_trace_of_itself`
  expected `psycopg.errors.DivisionByZero`. Behavior makes
  `migrate_postgres` raise `StoreServerError`, so each now asserts that
  class and the server's message. What each proves about rollback is
  unchanged.
- **The scrub is also proved on generated passwords.**
  `services/api/tests/test_api_exposure.py::test_a_generated_postgres_password_leaves_no_part_behind`
  makes passwords at run time, one written raw with an `@` and a `:` in it
  and one percent-encoded from characters a URL cannot carry raw, under
  both schemes in lower and mixed case. No 8-character window of either
  form survives.
- **The session lock's release.** Built first as an unlock skipped on a
  closed connection. The privacy review of PR #281 showed `closed` stays
  false until the next statement, so the release is now best effort (the
  amendment below).
- **Spec 112 cites the two tests as built.** Its acceptance criteria named
  them as planned, and the citation check fails on a planned test that
  exists, so `specs/112-the-tracker-domain-runs-on-either-store.md` now
  cites each by file and says spec 128 built it. No other text there
  changes.

## Amendment (2026-10-11): the privacy review of PR #281

A local privacy review of PR #281 found three gaps in what this spec
built, and two places where the spec claimed more than the code checks.
Behavior, Failure modes, Acceptance criteria and Honest limitations above
now say what the code does.

- **Raw whitespace in a password.** `store_target` accepted a password
  holding a raw no-break space, vertical tab, form feed, U+001C, U+0085,
  U+2028 or U+3000, and `scrub_secrets` left it whole. `store_target` now
  refuses raw whitespace and category C
  (`services/api/src/harrier/connection.py`, `_has_raw_space_or_control`).
  Two cases of
  `services/api/tests/test_postgres_store.py::test_a_url_libpq_cannot_parse_never_reaches_the_cli`,
  a raw space and a raw surrogate, are now refused before libpq reads the
  URL, so they print the text that names the fix instead of the plain
  refusal. Both are still refused and quote nothing of the URL.
- **`sslpassword` and the keyword/value form.** The query rule named
  `password`, and its `\b` failed inside `sslpassword`. Any parameter
  whose name holds `pass` or `secret` is now masked
  (`services/api/src/harrier/sources/__init__.py`,
  `_scrub_secret_parameters`). The scan reads each name once, so a long
  run of name characters costs linear time.
- **The unlock raised over the result.** It is now best effort
  (`services/api/src/harrier/pgstore.py`, `_release_the_session_lock`).
- **The contract.** It now names `psycopg_binary` too, and Behavior says it
  reads statements, not imports made at run time.
- **Shapes masked cheaply.** The `+driver` scheme, JSON-escaped slashes
  and a URL glued to the word before it are masked now. The shapes that
  are not are listed under Honest limitations.
- **Out of this spec.** The log stream and `HTTPException(detail=...)`
  sites do not run `scrub_secrets`. Nothing carries the store URL there
  while only `harrier store` runs on Postgres, so the gap belongs to spec
  112, which lets domain code run there. It is an acceptance criterion of
  that spec.

## Amendment (2026-10-11): the CodeRabbit review of PR #285

Two findings, both reproduced before the fix.

- **A percent-encoded parameter name.** libpq percent-decodes a query
  parameter's name, so `?%70assword=` sets the password, and store_target
  accepted that URL. `scrub_secrets` read the name as written and left the
  value whole. It now treats `%` as a name character and classifies the
  name after percent-decoding it
  (`services/api/tests/test_api_exposure.py::test_every_secret_named_parameter_is_scrubbed`,
  with `%70assword`, `ssl%70assword`, `PASS%57ORD` and
  `oauth_client_%73ecret` added).
- **Whitespace around the URL.** store_target stripped the value before
  checking it, so a URL with a trailing space was accepted with the space
  removed, a password's last character included. Whitespace around a
  Postgres URL is now refused with the text that names the fix, and a
  value of whitespace alone still means SQLite
  (`services/api/tests/test_postgres_store.py::test_whitespace_around_the_url_is_refused_not_stripped`).
  A `.env` value is still stripped by `harrier.envfile.read_env_file`, so
  only a process environment variable can carry the whitespace.
