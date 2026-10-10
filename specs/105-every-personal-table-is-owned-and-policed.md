---
spec: 105
title: Every personal table is owned and policed
status: accepted
approved: yes
milestone: M10
depends: [103]
---

# Spec 105: Every personal table is owned and policed

## Problem

The Postgres store from spec 103 has the local tracker's shape and nothing
more. No row belongs to anyone. Once two people use one hosted store,
their rows sit in the same tables. ADR-013 decision 1 puts an `owner_id`
on every table that holds personal data and forces a row-level policy on
it, so the database refuses a cross-owner read even when code forgets.
ADR-012 point 2 says why it must be the database: the tenant boundary is
the database's row policy, applied from the session, never a predicate the
domain writes.

Adding the column is the small part. The baseline was written for one
person, and several of its rules assume it:

- Five uniqueness rules are global. `idx_jobs_url`,
  `idx_jobs_external_key`, `tracks.slug UNIQUE`, `user_config UNIQUE
  (kind)` and `profile_documents UNIQUE (kind, name)`
  (`services/api/src/harrier/tracker/schema.py:470-471`, `:436`, `:501`,
  `:492`) would refuse owner B's row because owner A has one like it.
  Two people may track the same posting URL. Worse, the refusal itself
  tells B that someone else has that URL: Postgres checks unique and
  foreign key constraints without row security, and its manual warns of
  exactly this covert channel.
- `job_runs` has `job` as its primary key (`schema.py:505-508`), so two
  owners' scheduled runs would overwrite one success time.
- The baseline seeds track `id = 1` with no owner (`schema.py:443`), and
  `jobs.track_id` defaults to 1 (`schema.py:464`). The domain treats
  id 1 as "the default track" (`DEFAULT_TRACK_ID`,
  `services/api/src/harrier/tracks.py:36`) in six places
  (`tracks.py:204`, `:333`, `:344`,
  `services/api/src/harrier/digest.py:162`, `:165`,
  `services/api/src/harrier_api/tracks_routes.py:82`). With one global id 1, only
  one owner could have a default track.
- `jobs.track_id REFERENCES tracks(id)` and `job_events.job_id REFERENCES
  jobs(id)` (`schema.py:464`, `:513`) are checked without row security.
  Owner A could file a job under owner B's track, or an event against B's
  job, and the foreign key would accept it.

Two rules of the repo shape how this lands:

- Spec 103 requires every migration after the baseline to declare both
  dialects, and `undeclared_dialects` (`schema.py:544-564`) fails a version
  with an empty list. ADR-012 point 3 says nothing in the local schema
  names a tenant. An owner column is hosted only, so the SQLite side of
  this migration must be empty on purpose, and the repo has no way to say
  that yet.
- The parity test (`services/api/tests/test_dialect_parity.py`) requires
  the two stores to have the same columns, primary keys and unique
  constraints. After this spec they differ by design. The differences
  must be declared, not skipped.

## Scope

1. **Migration 10, hosted only.** `POSTGRES_MIGRATIONS` in
   `services/api/src/harrier/tracker/schema.py` gains version 10 with the
   statements under Behavior. `MIGRATIONS` gains `(10, [])`, declared
   hosted only (item 2). The SQLite runner records version 10 and changes
   nothing else in a local database.
2. **A declared way to say "SQLite: intentionally nothing".**
   `schema.py` gains `HOSTED_ONLY_MIGRATIONS: dict[int, str]`, version to
   reason. `undeclared_dialects` accepts an empty SQLite list only for a
   version in that mapping with a non-empty reason, and refuses a version
   in the mapping whose SQLite list is not empty. An empty Postgres list
   is refused as today; there is no local-only escape.
3. **The owned tables are declared in one place.** `schema.py` gains
   `OWNED_TABLES` (the seven tables below) and `UNOWNED_TABLES`, a mapping
   of table to reason (`schema_version`: migration bookkeeping, no
   personal data). It also declares, for the parity test, each hosted-only
   column and each key that is per owner on Postgres and global on SQLite
   (`HOSTED_ONLY_COLUMNS`, `OWNER_SCOPED_KEYS`). ADR-013 decision 5 asks
   for hosted-only columns and policies to be "part of that definition,
   marked hosted". This is the marking.
4. **The parity test reads the declarations.** It removes declared
   hosted-only columns before comparing columns, maps each declared
   per-owner key to its SQLite form before comparing keys, and fails if a
   declaration names a column or key that does not exist on either side.
   Its Postgres side runs with one synthetic owner's claims set, so
   `owner_id` defaults resolve.
5. **A test shim stands in for Supabase's auth objects.**
   `services/api/tests/pg_support.py` installs, in every fresh test
   database before migrating: the roles `anon`, `authenticated` and
   `service_role`, a schema `auth`, a table `auth.users (id uuid primary
   key)`, and a function `auth.uid()` with the body Supabase's auth server
   ships. It also gives tests one way to act as an owner (see Behavior).
   The shim is test code. No migration creates any of it.
6. **New tests** for the catalog, isolation, per-owner uniqueness, the
   same-owner references, the owner's first track and migration 10's
   refusals, in a new `services/api/tests/test_owner_policy.py`, plus the
   changes to existing Postgres tests that the new version number causes
   (`postgres 0 -> 9` becomes `postgres 0 -> 10`).
7. **ADR-013 decision 1 gains an "Amended (spec 105)" note** with the text
   under Behavior.
8. **`docs/architecture.md`** names the owner column, the policy, the
   catalog test and the shim in its hosted section.
9. **Findings that belong to other specs are written into them** (specs
   only, no code): spec 104's `depends` gains 105 and its stub gains the
   bypass criterion under Failure modes; spec 110's stub gains the Data API
   exposure criterion; spec 111's stub gains that deleting a user with
   rows is refused; spec 112's stub gains the conflict-target constraint;
   specs 106 and 107 gain that a track id is no longer unique across
   owners.

## Behavior

### Who owns what

| Table | Owned | Why |
|---|---|---|
| `jobs` | yes | the tracker rows |
| `contacts` | yes | people and their emails |
| `profile_documents` | yes | truth sources, resume bundle |
| `user_config` | yes | watchlist, searches, hold list |
| `job_events` | yes | every decision with its reason |
| `tracks` | yes | an owner's searches |
| `job_runs` | yes | per-owner scheduled run state; its content is not personal (`schema.py:203-205`), but each owner's runs succeed separately |
| `schema_version` | no | migration bookkeeping |

Every owned table gets, on Postgres only:

```sql
owner_id uuid NOT NULL DEFAULT auth.uid() REFERENCES auth.users (id)
```

The foreign key has no `ON DELETE` action, so deleting an auth user who
still owns rows is refused. Spec 111 decides deletion.

### Uniqueness becomes per owner

| Rule today (both dialects) | Postgres after migration 10 | SQLite |
|---|---|---|
| `idx_jobs_url ON jobs(url) WHERE url <> ''` | `(owner_id, url) WHERE url <> ''` | unchanged |
| `idx_jobs_external_key ON jobs(external_key) WHERE external_key <> ''` | `(owner_id, external_key) WHERE external_key <> ''` | unchanged |
| `tracks.slug UNIQUE` | `UNIQUE (owner_id, slug)` | unchanged |
| `user_config UNIQUE (kind)` | `UNIQUE (owner_id, kind)` | unchanged |
| `profile_documents UNIQUE (kind, name)` | `UNIQUE (owner_id, kind, name)` | unchanged |
| `job_runs PRIMARY KEY (job)` | `PRIMARY KEY (owner_id, job)` | unchanged |
| `tracks PRIMARY KEY (id)` | `PRIMARY KEY (owner_id, id)` | unchanged |
| (none) | `jobs UNIQUE (owner_id, id)`, target of `job_events`' reference | none |

Each per-owner rule leads with `owner_id`. That makes it the index the
policy's filter uses, which is the performance advice Supabase gives for
policy columns. `contacts` and `job_events` have no key of their own, so
each gains a plain btree index on `owner_id`.

### Each owner has their own track 1

Track ids become per owner. `tracks.id` stops being an identity column on
Postgres. A `BEFORE INSERT` trigger, `tracks_number_per_owner`, sets `id`
when the insert gives none: one more than the owner's highest id, under a
transaction-scoped advisory lock keyed by the owner, so two tracks created
at once by one owner get two ids. An explicit id is kept, and the primary
key `(owner_id, id)` refuses a duplicate.

So every owner's default track is id 1, as it is locally. `jobs.track_id
DEFAULT 1` keeps its meaning: the inserting owner's track 1. The domain's
`DEFAULT_TRACK_ID = 1` keeps its meaning, because under the policy
`SELECT ... FROM tracks WHERE id = 1` can only find the reader's own
track 1. No domain line changes.

An owner's track 1 is created by the database when the owner is created:

- An `AFTER INSERT` trigger on `auth.users`, calling a `SECURITY DEFINER`
  function `harrier_new_owner()` with `search_path = ''`, inserts
  `(owner_id, id, slug, kind, label) = (NEW.id, 1, 'job', 'industry',
  'Job search')`. These are the values migration 8 seeds locally
  (`schema.py:333-334`). Supabase documents this trigger pattern for
  per-user rows and warns that a failing trigger blocks sign-up.
- Migration 10 inserts the same row for every user already in
  `auth.users`.
- `EXECUTE` on `harrier_new_owner()` is revoked from `PUBLIC`, `anon`,
  `authenticated` and `service_role`.

### References stay inside one owner

- `jobs (owner_id, track_id) REFERENCES tracks (owner_id, id)` replaces
  `jobs.track_id REFERENCES tracks(id)`.
- `job_events (owner_id, job_id) REFERENCES jobs (owner_id, id)` replaces
  `job_events.job_id REFERENCES jobs(id)`.

Both sides of each reference carry the row's own `owner_id`, which the
policy forces to be the session's. A job naming another owner's track, or
an event naming another owner's job, finds no referenced row and is
refused with the same foreign key error as an id that does not exist. The
refusal says nothing about whether the other row exists.

### The policy

On each owned table:

```sql
ALTER TABLE <t> ENABLE ROW LEVEL SECURITY;
ALTER TABLE <t> FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON <t>
    AS PERMISSIVE FOR ALL TO authenticated
    USING (owner_id = (SELECT auth.uid()))
    WITH CHECK (owner_id = (SELECT auth.uid()));
```

- One policy for every command, as ADR-013 decision 1 says. `USING`
  decides which rows a select, update or delete can see. `WITH CHECK`
  decides which rows an insert or update may store. Postgres would reuse
  `USING` as the check on a `FOR ALL` policy without one; it is written
  out so a reader does not need that rule.
- `TO authenticated`: the policy names the only role it serves. Supabase's
  guidance is to always name the role.
- `(SELECT auth.uid())` rather than `auth.uid()`: Supabase documents that
  the wrapped form runs once per statement as an init plan instead of once
  per row.
- `auth.uid()` is null without a user. `owner_id = NULL` is never true, so
  a session without claims sees no rows and cannot store any.
- `FORCE` subjects the table owner to the policy. It does not affect a
  superuser or a role with `BYPASSRLS`. On Supabase the `postgres` role,
  which runs migrations and owns the tables, has `BYPASSRLS`, and so does
  `service_role`. See Honest limitations.

`schema_version` gets row security enabled and forced too, with one
policy `version_readable FOR SELECT TO authenticated USING (true)`. Every
table in the schema then has row security on, with no exception to argue
about.

### Grants

Migration 10 does not rely on any default privilege. Supabase granted
every privilege on new `public` tables to `anon`, `authenticated` and
`service_role` on older projects, and changes that default for all
projects on 2026-10-30. Either way, migration 10 states the result:

- `REVOKE ALL` on every table in `OWNED_TABLES` and `UNOWNED_TABLES`, and
  on their sequences, from `PUBLIC`, `anon`, `authenticated` and
  `service_role`.
- `GRANT SELECT, INSERT, UPDATE, DELETE` to `authenticated` on `jobs`,
  `contacts`, `profile_documents`, `user_config` and `job_runs`.
  `GRANT SELECT, INSERT` on `job_events` and `SELECT, INSERT, UPDATE` on
  `tracks`, where the existing triggers refuse the rest anyway.
  `GRANT SELECT` on `schema_version`.
- `anon` gets nothing on any table, sequence or harrier function.
- Assumption: inserting into an identity column needs no privilege on its
  sequence, so `authenticated` gets none. Not confirmed from the
  documentation; (`services/api/tests/test_owner_policy.py::test_an_owner_files_a_job_with_the_defaults`)
  proves it or fails.
- `service_role` gets nothing. It bypasses row security, so a table
  privilege is the only thing that limits it. A later spec whose operator
  command needs a table (spec 108's due-owner list, spec 111's export)
  grants that privilege in its own migration, where it is reviewed as a
  bypass.

### How migration 10 runs

In one transaction, under spec 103's advisory lock:

1. **Precheck Supabase's auth objects.** If `auth.users`, `auth.uid()` or
   the role `authenticated` is missing, raise `migration 10 needs
   Supabase's auth schema (auth.users, auth.uid()) and the authenticated
   role; this store has none`.
2. **Precheck that nothing is ownerless.** If any owned table holds a row,
   other than the baseline's seeded track 1, raise `migration 10 found rows
   in <table> with no owner; it cannot choose one`. No owner is guessed.
3. Remove the seeded ownerless track. The `tracks_are_never_deleted`
   trigger is disabled for that one statement and enabled again in the
   same transaction.
4. Add `owner_id` to each owned table, rebuild the keys in the uniqueness
   table, replace the two references, drop identity from `tracks.id`, and
   create `tracks_number_per_owner`.
5. Create `harrier_new_owner()`, its trigger on `auth.users`, and seed
   track 1 for each existing user.
6. Enable, force and create the policies; apply the grants.

A refusal in step 1 or 2 rolls the transaction back and leaves the store
at version 9 (spec 090's rule, as spec 103 applies it).

### The hosted-only declaration

```python
HOSTED_ONLY_MIGRATIONS: dict[int, str] = {
    10: "owner_id and row-level policy (spec 105); "
        "the local schema names no tenant (ADR-012 point 3)",
}
```

`undeclared_dialects` reports, by version:

| Case | Result |
|---|---|
| SQLite list empty, version not declared hosted only | `migration N declares no sqlite statements` (as today) |
| SQLite list empty, version declared with a reason | accepted |
| Version declared hosted only, SQLite list not empty | `migration N is hosted only but declares sqlite statements` |
| Version declared hosted only with an empty reason | `migration N is hosted only without a reason` |
| Postgres list empty, any version | `migration N declares no postgres statements` (as today) |

### The parity test after this spec

- Tables: the same set in both stores, as today.
- Columns: SQLite's columns equal Postgres's columns minus
  `HOSTED_ONLY_COLUMNS`, in order. `owner_id` is appended last on
  Postgres, so order is unaffected.
- Keys: each Postgres key named in `OWNER_SCOPED_KEYS` is compared to its
  SQLite form with the leading `owner_id` removed. `jobs UNIQUE
  (owner_id, id)` is declared Postgres-only. A declared key missing on
  either side fails the test, as `KNOWN_NULLABILITY_DIFFERENCES` does.
- Defaults and probes: run on Postgres as the test superuser with one
  synthetic owner's claims set and that owner present in `auth.users`.
  The superuser bypasses the policy; the claims make `owner_id` default to
  that owner. Every probe in spec 103's table keeps its expected result.
  `track-without-id-after-seed` gets id 2 from the per-owner trigger and
  still passes.
- Not compared: policies, grants, row security flags. The catalog test
  owns those, on Postgres only.

### Acting as an owner in tests

`pg_support.py` gains a context manager `as_owner(conn, owner)` that opens
a transaction and runs:

```sql
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claims', '{"sub": "<owner>", "role": "authenticated"}', true);
```

Owners are random UUIDs inserted into the shim's `auth.users`, which
fires `harrier_new_owner()` and gives each one track 1. No personal data
is involved. This is the shape spec 104's request transaction takes
(ADR-013 decision 3), so the tests exercise the same mechanism a request
will.

The shim's `auth.uid()` reads `request.jwt.claim.sub`, then
`request.jwt.claims ->> 'sub'`, as `uuid`, which is what Supabase's auth
server migration defines. `authenticated` and `anon` are `NOLOGIN
NOBYPASSRLS`; `service_role` is `NOLOGIN BYPASSRLS`, as Supabase
documents it.

### The catalog test

(`services/api/tests/test_owner_policy.py::test_every_table_is_owned_or_declared`) lists every relation in
schema `public` from `pg_class` and fails, naming the table, when:

- a table is in neither `OWNED_TABLES` nor `UNOWNED_TABLES`;
- a declared table does not exist;
- an owned table lacks `owner_id uuid NOT NULL` with default `auth.uid()`
  and a foreign key to `auth.users (id)`;
- any table lacks row security enabled, or forced;
- an owned table's policies are not exactly one: `owner_only`, permissive,
  `ALL`, roles `{authenticated}`, with `USING` and `WITH CHECK` both equal
  to the expression above as Postgres prints it;
- an owned table has no non-partial btree index whose first column is
  `owner_id`;
- `anon` or `service_role` holds any privilege on any table or sequence
  (a later spec that grants `service_role` something adds it to the
  catalog's expected set in the same change);
- `authenticated`'s privileges on a table differ from the Grants list;
- `authenticated` has `BYPASSRLS` or owns a table;
- a view or materialized view exists in `public` (a view made by
  `postgres` runs with its owner's rights and skips the policy);
- a `SECURITY DEFINER` function exists in `public` other than
  `harrier_new_owner()`.

## Failure modes

- **A request connected as the table owner or as `service_role`.** Row
  security does not apply to a role with `BYPASSRLS`, so every owner's
  rows are visible and writable. The database cannot refuse this. The
  guard is ADR-013 decision 3 and a spec 104 test that a request's
  transaction runs as `authenticated` with `BYPASSRLS` false
  (written into spec 104's stub by this spec). This spec proves the bypass
  is real so that the rule rests on a fact
  (`services/api/tests/test_owner_policy.py::test_a_bypassrls_role_sees_every_owner`).
- **A session as `authenticated` with no claims.** `auth.uid()` is null:
  it reads zero rows, and an insert fails on `owner_id` NOT NULL. Fails
  closed.
- **An insert with no owner from `service_role` or `postgres`.** The
  default `auth.uid()` is null, the NOT NULL constraint refuses the row,
  and nothing is written. An operator command that writes rows must name
  the owner, and that is a bypass its own spec reviews.
- **An insert or update naming another owner.** Refused with Postgres's
  `new row violates row-level security policy for table "<t>"`. Nothing is
  written.
- **A read, update or delete of another owner's row by id.** Zero rows
  seen or changed, the same as an id that does not exist.
- **A job naming another owner's track, an event naming another owner's
  job.** Refused as a foreign key violation, the same refusal as an id
  that does not exist.
- **The same URL, slug, config kind, document name or run name for two
  owners.** Accepted for both. Twice for one owner: refused, as today.
- **A new table without `owner_id`, without row security, or without a
  policy.** The catalog test fails, naming the table. If one reached a
  store anyway: row security on with no policy denies everything (fails
  closed); row security off leaves the table open to any role with a
  grant, which is why migration 10 revokes and grants explicitly and why
  the catalog checks privileges as well as policies.
- **A permissive policy added beside `owner_only`.** Permissive policies
  combine with OR, so it would widen access. The catalog test refuses any
  second policy on an owned table.
- **A view or `SECURITY DEFINER` function added later.** Either can read
  past the policy. The catalog test fails on any view and on any
  undeclared definer function.
- **Migration 10 on a store with ownerless rows.** Refused with the
  message under Behavior, naming the table. The store stays at version 9.
- **Migration 10 on a Postgres without Supabase's auth objects.** Refused
  with the message under Behavior. `harrier store migrate` exits 1 and the
  store stays at version 9. Locally, the Postgres tests install the shim
  first.
- **Two tracks created at once by one owner.** The advisory lock in
  `tracks_number_per_owner` serializes them; they get two ids.
- **`harrier_new_owner()` fails.** The `auth.users` insert fails with it,
  so sign-up fails. An owner without track 1 cannot use the product, so
  failing sign-up is the honest result. Supabase documents this effect.
- **Deleting an auth user who owns rows.** Refused by the `owner_id`
  foreign key. Spec 111 decides how deletion works.
- **A tenant calls Supabase's Data API directly** with their own token and
  the anon key the SPA holds (ADR-013 decision 7). The `authenticated`
  grants make harrier's tables reachable through PostgREST if `public` is
  an exposed schema. The policy still confines the caller to their own
  rows and every CHECK and trigger still applies, but the write would skip
  `harrier.tracker`, the one write path (ADR-003). This spec does not
  close it; it writes into spec 110's stub that the deployed project must
  not expose the schema holding these tables, checked at deploy.

## Acceptance criteria

- [x] `POSTGRES_MIGRATIONS` has version 10 with the statements under
      Behavior; `MIGRATIONS` has `(10, [])`; `SINGLE_DIALECT_MIGRATIONS`
      declares 10 as Postgres only, with its reason (see Amendment).
- [x] `undeclared_dialects` returns each result in the hosted-only table
      under Behavior
      (`services/api/tests/test_postgres_store.py::test_a_hosted_only_migration_leaves_sqlite_empty_on_purpose`), and
      `services/api/tests/test_postgres_store.py::test_every_new_migration_declares_both_dialects`
      still passes; a SQLite-only version is accepted the same way
      (`services/api/tests/test_postgres_store.py::test_a_sqlite_only_migration_leaves_postgres_empty_on_purpose`).
- [x] A fresh SQLite store and one migrated from version 9 are at version
      10, and neither has a column named `owner_id`, `owner` or
      `tenant_id`, nor a table named `users`
      (`services/api/tests/test_owner_policy.py::test_the_local_schema_names_no_tenant`).
- [x] `harrier store migrate` on an empty shimmed Postgres database prints
      `postgres 0 -> 10`; a second run prints `postgres 10 -> 10`
      (`services/api/tests/test_store_cli.py::test_store_migrate_prints_before_and_after_on_postgres`
      and
      `services/api/tests/test_postgres_store.py::test_migrate_applies_the_baseline_once`,
      updated).
- [x] On a Postgres without the shim, `harrier store migrate` exits 1 with
      the auth-objects message and the store stays at version 9
      (`services/api/tests/test_owner_policy.py::test_migration_10_needs_supabase_auth`).
- [x] On a store at version 9 with a row in any owned table, migration 10
      is refused naming that table, and the store stays at version 9
      (`services/api/tests/test_owner_policy.py::test_migration_10_refuses_ownerless_rows`).
- [x] The catalog test passes, and fails in each of these three runs,
      recorded in the pull request: `FORCE` removed from one table, the
      policy dropped from one table, a table added without `owner_id`
      (`services/api/tests/test_owner_policy.py::test_every_table_is_owned_or_declared`).
- [x] For every owned table, owner A reads zero of owner B's rows, and A's
      update and delete of B's rows change zero rows
      (`services/api/tests/test_owner_policy.py::test_owner_a_reads_nothing_of_owner_b`).
- [x] For every owned table, A inserting a row with B's `owner_id`, and A
      updating its own row's `owner_id` to B's, are refused with the row
      security message (`services/api/tests/test_owner_policy.py::test_owner_a_cannot_write_as_owner_b`).
- [x] An insert with no claims is refused: as `harrier_tenant` by the row
      policy, as `service_role` by its missing privilege, and as the
      migrating superuser by `owner_id` NOT NULL (see Amendment)
      (`services/api/tests/test_owner_policy.py::test_an_insert_without_an_owner_is_refused`).
- [x] For each of the first seven rules in the uniqueness table, the
      same value is accepted for two owners and refused twice for one
      (`services/api/tests/test_owner_policy.py::test_uniqueness_is_per_owner`).
- [x] A job naming another owner's track, and an event naming another
      owner's job, are refused; the error class is the same as for an id
      that exists nowhere (`services/api/tests/test_owner_policy.py::test_references_stay_inside_one_owner`).
- [x] Inserting a user into `auth.users` gives that owner track 1 with slug
      `job`; two owners both have id 1; an owner's next track gets id 2;
      two concurrent inserts for one owner get distinct ids
      (`services/api/tests/test_owner_policy.py::test_every_owner_starts_with_track_one`).
- [x] As owner A, `INSERT INTO jobs DEFAULT VALUES` stores a row with
      `owner_id` A and `track_id` 1, so the default and the composite
      reference work for `harrier_tenant` with no sequence privilege
      (`services/api/tests/test_owner_policy.py::test_an_owner_files_a_job_with_the_defaults`).
- [x] A role with `BYPASSRLS` reads both owners' rows
      (`services/api/tests/test_owner_policy.py::test_a_bypassrls_role_sees_every_owner`).
- [x] `services/api/tests/test_dialect_parity.py::test_both_dialects_build_the_same_tracker`
      passes with the declared differences, and fails when one
      `OWNER_SCOPED_KEYS` entry names a key that does not exist.
- [x] Every probe in
      `services/api/tests/test_dialect_parity.py::test_a_probe_is_refused_or_accepted_on_both`
      keeps its expected result on both dialects.
- [x] No migration statement creates the `auth` schema, `auth.uid()` or a
      Supabase role; only `services/api/tests/pg_support.py` does.
      Migration 10 creates one role, `harrier_tenant`, when absent (see
      Amendment).
- [x] Acting as `authenticated` with valid claims, the role Supabase's Data
      API uses, every owned table refuses select, insert, update and delete
      (`services/api/tests/test_owner_policy.py::test_the_data_api_role_reaches_no_harrier_table`).
- [x] `TRUNCATE job_events` and `TRUNCATE tracks CASCADE` are refused, even
      for the superuser, and leave every row
      (`services/api/tests/test_owner_policy.py::test_truncate_cannot_empty_events_or_tracks`).
- [x] The catalog test names each of its breaches
      (`services/api/tests/test_owner_policy.py::test_the_catalog_names_each_breach`),
      and a stale declaration fails the parity test
      (`services/api/tests/test_dialect_parity.py::test_a_declaration_naming_nothing_fails`).
- [x] `services/api/src/harrier/tracker/schema.py` is still the only file
      holding Postgres DDL, and no file under `services/api/src/harrier/`
      other than it names `owner_id`.
- [x] ADR-013 decision 1 carries the "Amended (spec 105)" note.
- [x] Specs 104, 106, 107, 110, 111 and 112 carry the lines named in Scope
      item 9, and nothing else in them changes.
- [x] CI's `check-python` job runs every new test against Postgres (run
      38066518468 on PR #216: 2608 passed, none skipped).
- [x] `just check` passes.

## Honest limitations

- **`FORCE` does not police Supabase's own powerful roles.** Postgres
  exempts superusers and `BYPASSRLS` roles from row security whatever
  `FORCE` says. Supabase documents `service_role` and the `postgres` role,
  which owns the tables, as having `BYPASSRLS`. `FORCE` matters only for a
  table owner without that attribute. It is kept because ADR-013 requires
  it and it costs nothing, not because it protects against `postgres`.
  The real guard against a bypassing connection is spec 104.
- **The shim is not Supabase.** The tests prove the policy, the keys and
  the references on plain Postgres with a hand-built `auth` schema. They
  do not prove: that the deployed `auth.uid()` has the body the shim
  copies; Supabase's role attributes and default privileges on the
  project's version; whether Supabase's own default privileges re-grant
  on future objects (a community thread reports one case it could not
  resolve); that a trigger on `auth.users` may be created by the role that
  runs migrations; that a foreign key check against `auth.users` succeeds
  when `authenticated` inserts, given that Supabase's auth role owns that
  table; and anything about connection pooling. A run of the isolation
  tests against a real staging project is spec 110's.
- **Ids still leak volume.** `jobs`, `contacts`, `profile_documents`,
  `user_config` and `job_events` keep one global identity sequence each.
  An owner who sees ids 10 and 40 knows rows were created between them.
  No content leaks. Per-owner ids for every table would close it, at the
  cost of a numbering trigger on each; not done.
- **The catalog compares policy text as Postgres prints it.** A Postgres
  major version that prints the expression differently fails the test
  without a real change. The isolation tests are the behavioral proof;
  the text check is what catches a second policy or a changed one.
- **No performance claim.** The policy follows Supabase's two documented
  pieces of advice (wrap `auth.uid()`, index `owner_id`). Nothing is
  measured, and the existing non-owner indexes are not re-ordered.
- **Covert channels beyond keys and references** (timing, error
  differences in domain code) are not examined.
- **The Data API path is open until spec 110 closes it** (Failure modes).
- **Track ids are no longer unique across owners.** Anything keyed by a
  track id alone would mix owners: `harrier.academic.discovery` builds a
  run key and data paths from it
  (`services/api/src/harrier/academic/discovery.py:71`, `:153`, `:172`).
  Locally nothing changes. Hosted, specs 106 and 107 must qualify those
  keys by owner; this spec writes that into their stubs.

## Migration

None for the local product. SQLite records version 10 and changes no
table; a fresh clone, the demo and every local command behave as before.

A hosted store reaches version 10 through `harrier store migrate`. Until
spec 112 nothing writes rows to it (spec 103 item 7), so migration 10's
ownerless-rows precheck holds on every store that exists. A store that
somehow has such rows is refused, not guessed at.

## Options weighed

- **One `FOR ALL` policy, or four per command.** Supabase advises one
  policy per command, because `FOR ALL` hides which rule covers what. Here
  every command has the same rule, written out with both clauses, and
  ADR-013 decision 1 says "one policy". Four identical policies would be
  four places to drift. Chosen: one.
- **A restrictive owner policy plus a permissive "allow" policy.** It
  would keep an added permissive policy from widening access. It doubles
  the policies to protect against something the catalog test already
  refuses. Not chosen.
- **Global track ids with a default-track marker.** Keep `tracks.id` an
  identity, add `UNIQUE (owner_id, id)` for the reference, and mark each
  owner's default track with a column. `jobs.track_id DEFAULT 1` would
  have to become a function looking up the marker, and the six places the
  domain compares with `DEFAULT_TRACK_ID` would change in both dialects.
  Not chosen: per-owner ids keep the domain and the local schema
  unchanged.
- **A same-owner trigger instead of composite references.** A trigger can
  check that `track_id` belongs to the row's owner. A composite foreign
  key says it declaratively, cannot be disabled by forgetting a trigger on
  update, and gives the same error as a missing row. Chosen: composite
  keys.
- **Creating track 1 on first use instead of on sign-up.** The domain
  would insert track 1 when `default_scope` finds none. The SQL names no
  tenant, but it is a write on a read path, a new branch only Postgres
  takes, and a domain change. Creating it in spec 111's sign-up flow
  would put it in the API, where a user created another way (the Supabase
  dashboard) would have none. Not chosen; the trigger covers every way a
  user is created.
- **`SELECT 1` as migration 10's SQLite statement.** It would satisfy
  `undeclared_dialects` while hiding that the migration is hosted only.
  Not chosen: an empty list with a declared reason says it.
- **Skipping version 10 on SQLite.** The two version sequences would
  drift, and migration 11 would have to explain the gap. Not chosen.
- **Supabase's Postgres image in CI instead of a shim.** It would bring the
  real roles and, as far as can be told, an `auth` schema. What its
  version ships is not pinned by anything harrier controls, and the
  `auth.users` table and `auth.uid()` it carries are not documented as a
  stable test surface. Not chosen for now; open decision 5.
- **Moving the tables out of `public` now.** A schema the Data API does
  not expose would close the direct-PostgREST path in code. It changes
  spec 103's baseline schema, every unqualified table name's search path,
  and the parity test's catalog queries. Not chosen here; open
  decision 6.

## Open decisions for Akin

Akin took the recommended answer to each on 2026-10-10, including the
build order in decision 1: 103, then 105, then 112, then 104.

1. **Build order: 105 before 112 and 104.** The stub said `depends: [103,
   104]`. Nothing here needs authentication: the tests act as owners
   through the database. Landing 105 first means spec 112 ports the
   domain once, against the final keys, and spec 104 authenticates into
   tables that are already policed. Recommended: `depends: [103]`, and
   spec 104 depends on 105.

   One consequence lands in spec 112 either way. Three domain upserts
   name a conflict target that is global today: `ON CONFLICT(job)`
   (`services/api/src/harrier/runoutcome.py:148`), `ON CONFLICT (kind)`
   (`services/api/src/harrier/userconfig/store.py:171`) and `ON CONFLICT
   (kind, name)` (`services/api/src/harrier/profile/store.py:33`).
   Postgres matches a conflict target to a unique index with exactly those
   columns, and after migration 10 none exists. Writing `owner_id` into
   them would make domain SQL name a tenant (ADR-013 decision 2).
   Recommended for spec 112: update, then insert when no row changed, in
   one transaction, retrying the update once on a unique violation. It is
   one text for both dialects and names no tenant. If Akin keeps 112
   before 105, this spec makes that change to the three files instead.
2. **Per-owner track ids** (recommended), or global ids with a
   default-track marker (Options weighed).
3. **Track 1 created by a trigger on `auth.users`** (recommended), or on
   first use, or in spec 111's sign-up flow.
4. **One `FOR ALL` policy per table** (recommended), or four per-command
   policies as Supabase's guide prefers.
5. **Tests impersonate owners through a shim on plain Postgres**
   (recommended), with a run against a real staging project in spec 110.
   The alternative is Supabase's Postgres image in CI.
6. **Data API exposure goes to spec 110** (recommended): the deployed
   project does not expose `public`, checked at deploy. The alternative is
   moving harrier's tables to a non-exposed schema in this spec.
7. **`service_role` gets no table privilege here** (recommended); each
   later operator command grants what it needs in its own migration. The
   alternative is Supabase's default of full privileges.

## Proof / origin

- `docs/adr/ADR-013-hosted-multi-tenant-deployment.md`, decisions 1, 2, 3,
  5 and 7, and its Honest limitations on the service role.
- `docs/adr/ADR-012-search-tracks-and-tenant-isolation.md`, points 2 and 3.
- `docs/adr/ADR-009-user-configuration-and-tenancy.md`, point 3 as
  amended by `specs/041-smaller-review-corrections.md`: a tenant column is
  added when a tenant exists, and here one does.
- `specs/103-the-tracker-store-speaks-postgres.md`: the baseline, the
  both-dialects rule, the parity test, and its amendment naming
  `undeclared_dialects`.
- `services/api/src/harrier/tracker/schema.py`: every key, reference and
  seed listed under Problem.
- `services/api/src/harrier/tracks.py` and
  `services/api/src/harrier/digest.py`: the domain's uses of
  `DEFAULT_TRACK_ID`.
- PostgreSQL, Row Security Policies: who bypasses row security, `FORCE`,
  default deny, and referential checks bypassing row security (the covert
  channel): https://www.postgresql.org/docs/current/ddl-rowsecurity.html
- PostgreSQL, CREATE POLICY: `USING` and `WITH CHECK` per command, `FOR
  ALL` reusing `USING`, permissive policies combining with OR, `ON
  CONFLICT` and `RETURNING` needing `SELECT` policies:
  https://www.postgresql.org/docs/current/sql-createpolicy.html
- Supabase, Row Level Security: `auth.uid()` null without a user, `(select
  auth.uid())`, indexing policy columns, naming the role with `TO`,
  `service_role` and `postgres` having `bypassrls`, default grants on
  `public`: https://supabase.com/docs/guides/database/postgres/row-level-security
- Supabase changelog 45329, new tables not exposed to the Data API by
  default, applied to existing projects on 2026-10-30:
  https://supabase.com/changelog/45329-breaking-change-tables-not-exposed-to-data-and-graphql-api-automatically
- Supabase, Managing user data: the trigger on `auth.users` and the
  warning that a failing trigger blocks sign-up:
  https://supabase.com/docs/guides/auth/managing-user-data
- Supabase auth server, `auth.uid()`'s body (reads `request.jwt.claim.sub`,
  then `request.jwt.claims ->> 'sub'`):
  https://github.com/supabase/auth/blob/master/migrations/20220224000811_update_auth_functions.up.sql

## Out of scope

- JWT verification, the request role and the request transaction: spec
  104.
- Porting domain SQL to Postgres, including the three upserts' conflict
  targets if open decision 1 is taken: spec 112.
- Credentials and run isolation, including run keys built from a track id:
  spec 106.
- Storage object policies and file paths built from a track id: spec 107.
- The scheduler's service-role reads: spec 108.
- The Data API exposure setting and a staging run of these tests: spec
  110.
- Sign-up, export and deletion: spec 111.
- Per-owner ids for tables other than `tracks`.
- Re-ordering the existing non-unique indexes for owner-filtered queries.

## Amendment (2026-10-10, during implementation)

A post-merge architecture review of PR #207 found two design gaps here
before this spec was built, and a data integrity review found a third.
Akin approved all three on 2026-10-10. Implementation found the rest.

- **The policed role is `harrier_tenant`, not `authenticated`.**
  Supabase's Data API (PostgREST) switches to `authenticated`. Granting
  harrier's tables to it would have let a signed-in user write rows
  directly, past `harrier.tracker`, the one write path (ADR-003), with only
  a deploy-time setting (spec 110) to stop it. Migration 10 creates
  `harrier_tenant NOLOGIN NOINHERIT NOBYPASSRLS` when absent, tolerating a
  concurrent creator, since roles are cluster wide. `owner_only` is
  `FOR ALL TO harrier_tenant`; `version_readable` is `FOR SELECT TO
  harrier_tenant`. The Grants list applies to `harrier_tenant`. `PUBLIC`,
  `anon`, `authenticated` and `service_role` hold no privilege on any
  harrier table, sequence or function. Everywhere Behavior and Failure
  modes say a session or a policy uses `authenticated`, read
  `harrier_tenant`. A request reaches it by `SET LOCAL ROLE harrier_tenant`
  with the claims set; `auth.uid()` still reads the claims. Spec 104
  carries the request side.
- **`harrier_tenant` needs `auth`.** The policy evaluates `auth.uid()` as
  the session role, so migration 10 grants it `USAGE ON SCHEMA auth` and
  `EXECUTE ON FUNCTION auth.uid()`. Whether Supabase's migrating role may
  grant on a schema its auth service owns is unverified, like the trigger
  on `auth.users`.
- **One-dialect migrations go either way.** `SINGLE_DIALECT_MIGRATIONS`
  (version to the one dialect and a reason) replaces
  `HOSTED_ONLY_MIGRATIONS`. A SQLite-only fix, such as making
  `job_runs.job` NOT NULL (spec 103's corrections), needs the same escape.
  A declared version missing from the other list altogether is refused,
  since that runner would never record it.
- **TRUNCATE is refused.** The baseline's refusals are row triggers, which
  TRUNCATE does not fire, so `TRUNCATE job_events` and `TRUNCATE tracks
  CASCADE` emptied them for a privileged role. Migration 10 adds
  statement-level `BEFORE TRUNCATE` triggers with the same messages, and
  no grant includes TRUNCATE.
- **A refused migration exits 1.** The auth precheck raises inside the
  migration, which escaped `harrier store migrate` as a traceback.
  `services/api/src/harrier/pgstore.py`, which Scope does not name, gains
  `StoreMigrationRefused`, and the command prints `error: migration 10
  needs Supabase's auth schema (auth.users, auth.uid()) and the
  authenticated role; this store has none`. The precheck also requires
  `anon` and `service_role`, which the revokes name; the message is the
  spec's and names only `authenticated`.
- **A no-claims insert is refused by the policy first.** Postgres checks
  `WITH CHECK` before constraints, so as `harrier_tenant` the refusal is
  the row security message, not `owner_id` NOT NULL. `service_role` is
  refused by missing privilege. NOT NULL refuses the superuser. It fails
  closed every way.
- **Missing grants refuse before the policy.** As an owner, update and
  delete on `job_events` and delete on `tracks` fail with `permission
  denied`, since no grant allows them.
- **Every harrier function is revoked from every grantee,** not only
  `harrier_new_owner()`. A trigger fires without EXECUTE on its function.
- **An identity insert needs no sequence privilege,** as Behavior assumed.
- **Names the spec left open:** `harrier_number_track()` behind trigger
  `tracks_number_per_owner`, and `harrier_new_owner` on `auth.users`. The
  numbering lock is `pg_advisory_xact_lock(hashtext('harrier.tracks'),
  hashtext(owner_id::text))`, which never meets the runner's one-key lock.
  `OWNED_TABLES` lists `job_events` before `jobs`, so the ownerless
  precheck names the table that holds the row.
- **Forced row security on `schema_version` needs a bypassing migrator.**
  A role that owns the tables but is neither superuser nor `BYPASSRLS`
  would read version 0 and could not record one. Supabase's `postgres`
  has `BYPASSRLS`, and the tests run as superuser; spec 110's staging
  check confirms the first.

