---
spec: 115
title: Spec 105 holds on a real Supabase project
status: accepted
approved: yes
milestone: M10
depends: [103, 105]
---

# Spec 115: Spec 105 holds on a real Supabase project

Split from spec 110 on 2026-10-10. This text was spec 110's "Part A",
the first slice, moved here whole. Spec 110 keeps the deploy pipeline and
depends on this spec. Spec 112's second pull request (its behavior) depends
on this spec too: 112's `depends` gains 115 by amendment when this spec is
approved (Changes to approved specs). Spec 112 is not edited here.

## Problem

Spec 105 is proven only against a shim. CI's Postgres is plain
`postgres:17` (`.github/workflows/ci.yml`, job `check-python`) with
Supabase's roles, an `auth.users` table and `auth.uid()` imitated by
`services/api/tests/pg_support.py:78-131`. Every Postgres test connects as
the CI superuser and builds a fresh database per test
(`pg_support.py:183-208`), so no test can show a grant that a
non-superuser migrator lacks.

Unchecked on a real project:

- the migrating role can create a trigger on `auth.users`
  (`services/api/src/harrier/tracker/schema.py:979-981`);
- it can grant `harrier_tenant` usage on schema `auth` and execute on
  `auth.uid()` (`schema.py:1032-1033`);
- the deployed `auth.uid()` reads `sub` from `request.jwt.claims`;
- Supabase's `postgres` has `BYPASSRLS`, which the forced policy on
  `schema_version` needs (spec 105, Amendment);
- the pooler accepts a custom login role;
- the foreign key from `owner_id` to `auth.users` holds on insert as
  `harrier_tenant`;
- the migrating role and the request role resolve unqualified table names
  the same way.

Spec 105 left a run of its isolation tests against a real Supabase project
to spec 110 (its Honest limitations: "The shim is not Supabase"). Spec
104's Honest limitations and assumptions name the same run "spec 110's
first slice", which lands before spec 112. Akin decided on 2026-10-10 that
this first slice, which migrates a real Supabase staging project to the
latest version and runs spec 105's isolation tests there, lands before
spec 112. (Post-merge review of PRs #194, #207 and #216, 2026-10-10:
architecture 3, data integrity 5.)

Line numbers are from `main` at `44ac0d7` (2026-10-10), except the two
`schema.py` ones above, refreshed to `main` at `524dd8a` on 2026-10-11.

## Scope

1. The staging project exists (`eu-west-1`, Ireland): the existing
   Supabase project named `harrier`, which Akin created and which is
   staging for now. Its reference and URL stay out of every file. The
   agent creates no account and no project. Spec 110's "Region
   (2026-10-11)" holds the region decision.
2. `harrier store migrate` runs once against it as Supabase's own
   `postgres` role, from a shell with the URL exported, and prints
   `postgres 0 -> <target_version()>`.
3. A new test module, `services/api/tests/test_supabase_project.py`, holds
   one test per assumption under Behavior (A1 to A11). It runs only when
   `HARRIER_SUPABASE_TEST_URL` (the migrator) and
   `HARRIER_SUPABASE_API_TEST_URL` (a custom login role through the pooler)
   are set, and skips otherwise, in CI too (Honest limitations).
4. `services/api/tests/pg_support.py` gains a mode in which the `store` and
   `store_url` fixtures of `services/api/tests/test_owner_policy.py` yield
   the Supabase project instead of a fresh shimmed database. The tests
   listed under Behavior run in that mode; the others are listed with the
   reason they cannot.
5. CI migrates as a non-superuser. `pg_support.py` gains a role
   `harrier_migrator` with `CREATEROLE BYPASSRLS NOSUPERUSER`, given on the
   shim exactly the privileges the staging run found Supabase gives
   `postgres` (A1, A2, A8). Every Postgres test migrates as it. The
   superuser stays only for creating and dropping test databases and
   installing the shim.
6. Findings are written down: an "Amendment (staging findings)" section in
   this spec with each assumption marked confirmed or refuted, the date,
   and the Postgres version; and one paragraph in `docs/architecture.md`'s
   hosted section. A refuted assumption stops this spec's acceptance and
   becomes its own spec (or an amendment of spec 105 with a new migration)
   before spec 112 is built.
7. **Fly checks for the hosting budget** (added 2026-10-11). Five
   assumptions about Fly, F1 to F5, that specs 106, 108 and 110 rest on
   since the hosting budget, probed on a throwaway Fly app Akin creates
   and recorded in the same Amendment (Hosting budget (2026-10-11)).

Classification: the new test module sits under `services/api/tests/`,
which `config/data-classification.json` already classifies as public. No
new directory is added. The project's URLs and passwords stay in the
operator's shell and never enter a file.

## Behavior

### Who runs what

The project exists (`eu-west-1`): the existing Supabase project named
`harrier`. Akin records its Postgres version. He exports the `postgres` role's session
pooler URL in a shell (never in `.env`) and runs `harrier store migrate`.
He creates one login role for A5 in the SQL editor: `CREATE ROLE
harrier_api_probe LOGIN NOINHERIT PASSWORD ...`, with a password generated
on his machine and kept out of the shell history. Then he runs the test
module with both URLs exported. The pull request carries the command
output with the project reference and every password removed.

### The assumptions

Each is one test in `services/api/tests/test_supabase_project.py`. Each
test asserts the value spec 105 relies on; a failure is a refuted
assumption, recorded, not retried.

| | Assumption | Checked by |
|---|---|---|
| A1 | The migrating role can create a trigger on `auth.users` | migration 10 applied, and `pg_trigger` holds `harrier_new_owner` on `auth.users` |
| A2 | It can grant `harrier_tenant` `USAGE` on `auth` and `EXECUTE` on `auth.uid()` | `has_schema_privilege` and `has_function_privilege` for `harrier_tenant` |
| A3 | `auth.uid()` reads `sub` from `request.jwt.claims` | as `harrier_tenant`, with only `request.jwt.claims` set, `auth.uid()` returns its `sub`; `pg_get_functiondef('auth.uid()'::regprocedure)` is recorded beside the shim's body (`pg_support.py:89-97`) |
| A4 | `postgres` is not a superuser, has `BYPASSRLS` and `CREATEROLE` | `pg_roles` for `current_user` |
| A5 | The pooler accepts a custom login role, and `SET LOCAL` holds inside a transaction through it | as `harrier_api_probe` through the session pooler (port 5432) and the transaction pooler (port 6543): `current_user`, then spec 104's read-back after `SET LOCAL ROLE harrier_tenant` and the claims |
| A6 | The foreign key from `owner_id` to `auth.users` holds on insert as `harrier_tenant` | `services/api/tests/test_owner_policy.py::test_an_owner_files_a_job_with_the_defaults` in staging mode |
| A7 | The migrator and the request role resolve unqualified names to `public` | `current_schemas(true)` and `to_regclass('jobs')::text` in both sessions, and each role's `rolconfig` |
| A8 | `postgres` may insert into `auth.users` (the tests' `new_owner`, `pg_support.py:133-138`) | one insert of a random UUID |
| A9 | No Supabase default privilege re-grants harrier's tables to `anon`, `authenticated` or `service_role` | `services/api/tests/test_owner_policy.py::test_every_table_is_owned_or_declared` in staging mode |
| A10 | `postgres` can grant `harrier_tenant` to a login role (Postgres 16 and later ask for the admin option, which the role's creator holds) | `GRANT harrier_tenant TO harrier_api_probe` succeeds |
| A11 | The store migrates to `target_version()` as a non-superuser migrator shaped like Supabase's `postgres` (`CREATEROLE`, `BYPASSRLS`, not superuser), including migration 14's `ALTER ROLE` reset and its `ALTER DEFAULT PRIVILEGES` revokes (decision D8, 2026-10-11) | the store reaches `target_version()` as `postgres`; `pg_default_acl` holds no default privilege of the migrating role, in `public` or global (no `IN SCHEMA`), that grants to `anon`, `authenticated` or `service_role`; and, inside a transaction that is rolled back, a function the migrating role creates in `public` is executable by none of `PUBLIC`, `anon`, `authenticated` and `service_role` |

Rows A2 and A3 are restated by "Amendment (2026-10-11): A2 and A3 after
migration 16", below, which replaces them.

A11 comes from a post-merge review of spec 105's migration 13. Its
`ALTER ROLE harrier_tenant` names `NOSUPERUSER`, which a role that is not
a superuser may not name, and its `ALTER DEFAULT PRIVILEGES` revoke covers
`public` only, missing the global defaults (no `IN SCHEMA`) and the
`EXECUTE` on functions that Postgres grants `PUBLIC` by default. Spec
105's second post-merge review corrected both in migration 14, not in
migration 13, so A11 checks that the store reaches `target_version()` as
`postgres`, whatever the latest migration is, and that the catalog
result holds.

In the test suite, every `migrate_postgres` call runs as the superuser
the tests connect as. Only `TENANT_ROLE_RESET`, migration 14's first
statement, runs as a role that is not one
(`services/api/tests/test_owner_policy.py::test_migration_14_resets_the_role_as_a_migrator_that_is_not_a_superuser`).
So no test shows the whole chain applying as a non-superuser; this
spec's run as `postgres`, and CI's `harrier_migrator` after it, are that
proof. And
`services/api/tests/test_owner_policy.py::test_a_failed_migration_leaves_no_trace_of_itself`
at version 14 does not fingerprint `pg_roles`: it shows the catalog of
the database unchanged, not the cluster-wide role's attributes.

A5 is the one spec 110's design leans on hardest: if the transaction
pooler drops `SET LOCAL`, spec 104's read-back answers 500 on every
request and the API must use the session pooler or a direct connection.
A refuted A2, A3 or A6 means spec 105's policy cannot evaluate on Supabase
as written.

### The isolation tests on the project

These run in staging mode, against the migrated project, connected as
`postgres`:

- `services/api/tests/test_owner_policy.py::test_every_table_is_owned_or_declared`
- `services/api/tests/test_owner_policy.py::test_owner_a_reads_nothing_of_owner_b`
- `services/api/tests/test_owner_policy.py::test_owner_a_cannot_write_as_owner_b`
- `services/api/tests/test_owner_policy.py::test_an_insert_without_an_owner_is_refused`
- `services/api/tests/test_owner_policy.py::test_the_data_api_role_reaches_no_harrier_table`
- `services/api/tests/test_owner_policy.py::test_uniqueness_is_per_owner`
- `services/api/tests/test_owner_policy.py::test_references_stay_inside_one_owner`
- `services/api/tests/test_owner_policy.py::test_every_owner_starts_with_track_one`
- `services/api/tests/test_owner_policy.py::test_an_owner_files_a_job_with_the_defaults`
- `services/api/tests/test_owner_policy.py::test_a_bypassrls_role_sees_every_owner`
- `services/api/tests/test_owner_policy.py::test_truncate_cannot_empty_events_or_tracks`

Not run there, each for its reason: the two migration 10 refusal tests
need a store at version 9 or without the auth schema, and a project has
one database with Supabase's schema already in it; the catalog breach
test drops a policy and `FORCE`, which is a schema change to a shared
project; the SQLite test has nothing to do with Supabase.

`test_an_insert_without_an_owner_is_refused` asserts the superuser's
refusal by `owner_id` NOT NULL. As `postgres`, which bypasses the policy
but is not a superuser, the expected refusal is the same NOT NULL; the
test names the connecting role in its assertion message so the record
says which role was refused.

### What the run leaves behind

Owners are random UUIDs inserted into `auth.users`, with no email; each
gets track 1 from the trigger. Tracks are never deleted and events are
append only (spec 105), and the `owner_id` reference refuses deleting a
user who owns rows, so the synthetic owners and their rows stay. They
hold no personal data. Staging mode refuses to start, before it writes
anything, when `auth.users` holds any row with a non-null `email`: a
project anyone has signed in to, production included, is never a test
target. A later re-run uses a throwaway EU project created for it and
deleted after.

### CI migrates as a non-superuser

After the findings, every Postgres test's store is migrated by
`harrier_migrator`, a role with Supabase `postgres`'s attributes as A4
found them and, on the shim, the privileges A1, A2 and A8 found:
`TRIGGER` and `INSERT` on `auth.users`, `USAGE` on `auth` with grant
option, `EXECUTE` on `auth.uid()` with grant option. A missing grant in a
future migration then fails in CI, as it would on Supabase. If A4 finds
something else, the role takes what A4 found. The store migrates to
`target_version()` as `harrier_migrator` in CI, with the catalog result
A11 names (decision D8).

## Failure modes

- **A refuted assumption.** Its test fails with the observed value. This
  spec does not pass; the finding is recorded and a spec owns the fix
  before spec 112 is built. Nothing is worked around in the test.
- **Migration 10 refused on the project.** `harrier store migrate` exits 1
  with spec 105's text or `error: a migration failed: <primary message>`,
  and the project stays at version 9, whole (spec 090). Recorded as the
  finding for A1, A2 or A10.
- **Staging mode pointed at a used project.** Refused before any write,
  because `auth.users` holds a user with an email.
- **A URL variable missing.** The module skips, locally and in CI, and
  says which variable. This spec's acceptance needs the run's output in
  the pull request, so a skip cannot pass it.

## Acceptance criteria

- [ ] The pull request shows `harrier store migrate` on the staging project
      printing `postgres 0 -> <target_version()>`, run as `postgres`, with
      no project reference or password in the output.
- [ ] `services/api/tests/test_supabase_project.py` has one test per
      assumption A1 to A11 (`test_the_migrator_can_trigger_on_auth_users`,
      `test_the_tenant_role_reaches_auth_uid`,
      `test_auth_uid_reads_the_claims_sub`,
      `test_postgres_bypasses_but_is_not_superuser`,
      `test_the_pooler_keeps_a_custom_role_and_set_local`,
      `test_unqualified_names_resolve_to_public_for_both_roles`,
      `test_the_migrator_can_create_an_auth_user`,
      `test_the_migrator_can_grant_the_tenant_role`,
      `test_the_store_migrates_to_the_target_as_a_non_superuser_migrator`; A6 and A9 are the
      two existing tests named in the assumption table).
- [ ] The module skips, naming the variable, when either URL is unset
      (`test_supabase_checks_skip_without_a_project`).
- [ ] Staging mode refuses a project whose `auth.users` holds a user with
      an email, before any write
      (`test_staging_mode_refuses_a_project_with_real_users`).
- [ ] The eleven `test_owner_policy.py` tests listed under Behavior pass in
      staging mode against the project, and the pull request shows the
      run.
- [ ] Every Postgres test migrates as `harrier_migrator`, which is not a
      superuser, and CI's `check-python` run shows them passing, none
      skipped (`test_ci_migrates_as_a_non_superuser`).
- [ ] In CI, the store migrates to `target_version()` as
      `harrier_migrator`, and afterwards the migrating role holds no
      default privilege, in `public` or global, granting to `anon`,
      `authenticated` or `service_role`, and a function it creates is
      executable by none of them nor by `PUBLIC` (decision D8;
      `services/api/tests/test_owner_policy.py::test_the_store_migrates_to_the_target_as_a_non_superuser_migrator`, in CI form).
- [ ] This spec gains "Amendment (staging findings)" with A1 to A11 each
      marked confirmed or refuted, the date and the Postgres version, and
      `docs/architecture.md` says the isolation tests ran on a real
      project and names the module.
- [ ] The Fly checks F1 to F4 are recorded as "Hosting budget
      (2026-10-11)" states, each confirmed or refuted with its date
      (criteria under that section).
- [ ] No refuted assumption is left without a spec that owns its fix.
- [ ] `uv run ruff check` and `uv run pyright` pass on the service.
- [ ] `just check` passes.

## Honest limitations

- The module skips in CI. It needs a real project and its credentials,
  and CI holds neither; spec 039's rule (a gate that skips in CI has not
  run) applies, so this module is not a gate. It is a recorded check,
  re-run by hand on a throwaway project when Supabase changes. The lasting
  CI guard is the non-superuser migrator, which encodes what the run
  found.
- The staging project keeps synthetic owners and their rows after the
  run. No personal data, and the policy hides them from every other owner.
- A shim that copies Supabase's privileges as observed on one date drifts
  when Supabase changes them. Nothing notices until a re-run.
- The line numbers cited here are refreshed by the change that implements
  this spec, not before (decision D9, 2026-10-11).
- Whether the existing `test_owner_policy.py` fixtures can run against one
  shared Supabase database at all is not tested yet. Each test commits its
  rows, and synthetic owners cannot be deleted (the foreign key,
  append-only events, tracks never deleted). Staging mode and its refusal
  of a project with real users are this spec's design.

## Migration

None. No store changes: the staging project is migrated with the existing
`harrier store migrate`. CI's Postgres tests migrate as `harrier_migrator`
instead of the superuser, which changes no schema.

## Changes to approved specs

Each line below is a change this spec asks of an approved spec. None is
written into that spec here; each is an amendment Akin approves with this
spec.

- **Spec 112:** `depends` gains 115. Its second pull request (the
  behavior) is built after this spec's acceptance, as Akin decided on
  2026-10-10.
- **Spec 104:** its Honest limitations and assumptions name the staging
  run "spec 110's first slice" (the `\sf auth.uid` comparison and the
  pooler's acceptance of a custom login role). Both now read "spec 115".
- **Spec 105:** its Honest limitations name the run of its isolation tests
  against a real staging project, and the check that `postgres` has
  `BYPASSRLS`, as spec 110's. Both are this spec's.

## Decisions taken (2026-10-11)

Akin took these in chat on 2026-10-11 ("go with your recommendations").
The text above says where each changed it.

- **D8, migration 13 as a non-superuser migrator.** This spec also checks
  that migration 13 applies as a migrator shaped like Supabase's
  `postgres` (`CREATEROLE`, `BYPASSRLS`, not superuser), covering its
  `ALTER ROLE` step and its `ALTER DEFAULT PRIVILEGES` revoke, global
  defaults and `PUBLIC`'s function `EXECUTE` included. The fix is spec
  105's follow-up; this spec proves it on Supabase and in CI (A11, "CI
  migrates as a non-superuser", Acceptance criteria). The follow-up
  landed as migration 14 (spec 105, second post-merge review), so the
  check is that the store reaches `target_version()` as that migrator
  (post-merge review of PR #231).
- **D9, line numbers.** Refreshed by this spec's implementing change
  (Honest limitations).

Open decision 1 below was not the subject of these decisions; it stands
as written. Open decision 2 is added by the hosting budget of the same
day (Hosting budget (2026-10-11)).

## Open decisions for Akin

1. **The first slice as its own spec.** Recommended: this spec, approved
   and built now, before spec 112's second pull request; spec 110 keeps
   the pipeline, depends on 115, and is refined again once A1 to A10 are
   known, because A5 and A10 change its design if refuted. Alternative:
   the slice as phase 1 of spec 110, which means approving the pipeline
   before its assumptions are tested. 115 was the next free number when
   this split was made: `git ls-tree` over `main` and every remote branch
   after `git fetch origin`, 2026-10-10, found no `specs/115-*` to
   `specs/120-*` file.
2. **Where the Fly checks live.** Added 2026-10-11 by the hosting budget.
   Taken 2026-10-11 (Akin, "go with your recommendations"): the recommended answer.
   Recommended: here, as F1 to F5, because they gate the same specs this
   one gates and need no harrier code, only a probe app. Alternative: a
   spec of their own, which keeps this spec's title true ("Spec 105 holds
   on a real Supabase project") at the cost of one more spec in the
   chain.

## Proof / origin

- `specs/105-every-personal-table-is-owned-and-policed.md`: Honest
  limitations ("The shim is not Supabase"), Amendment (`harrier_tenant`,
  a bypassing migrator).
- `specs/104-the-hosted-api-authenticates-every-request.md`: the request
  transaction's read-back, Honest limitations and assumptions.
- `specs/110-deploy-pipeline-and-environments.md`: this text's origin, and
  the post-merge reviews it routed (architecture 3, data integrity 5).
- Code read on `main` at `44ac0d7`:
  `services/api/src/harrier/tracker/schema.py`,
  `services/api/tests/pg_support.py`,
  `services/api/tests/test_owner_policy.py`, `.github/workflows/ci.yml`.
- Supabase, Restricting access on auth, storage and realtime schemas
  (2025-04-21): functions may not be created in `auth`; triggers and RLS
  policies on `auth.users` and foreign keys to it stay allowed. Read
  2026-10-10.
  https://supabase.com/changelog/34270-restricting-access-on-auth-storage-and-realtime-schemas-on-april-21-2025
- Supabase, Security patch notice: entities in `auth` are owned by
  `supabase_admin` and "explicitly granted all permissions to `postgres`";
  `postgres` gains some superuser-only abilities, so it is not a
  superuser. Grant option not stated (A2). Read 2026-10-10.
  https://supabase.com/changelog/9314-security-patch-notice
- Supabase, Row Level Security: "On Supabase the owner is `postgres`,
  which has `bypassrls`". Read 2026-10-10.
  https://supabase.com/docs/guides/database/postgres/row-level-security
- Supabase, Postgres roles: `postgres` "has admin privileges"; a separate
  login role per service. Attributes not listed (A4). Read 2026-10-10.
  https://supabase.com/docs/guides/database/postgres/roles
- Supabase, Connecting to Postgres: direct connections are IPv6 unless the
  IPv4 add-on; the shared pooler is IPv4, session mode on 5432 keeps
  session state, transaction mode on 6543 has no prepared statements;
  custom roles connect to the pooler as `[ROLE].[PROJECT-REF]`. Read
  2026-10-10.
  https://supabase.com/docs/guides/database/connecting-to-postgres
- Supabase, Regions: the generic "Europe" choice may land in London or
  Zurich, so a named region is chosen. Read 2026-10-10.
  https://supabase.com/docs/guides/platform/regions

Assumptions this spec exists to check, each named in the table:

- `postgres` holds grant option on schema `auth` and on `auth.uid()` (A2).
- `auth.uid()`'s deployed body (A3). Third-party write-ups disagree; spec
  105 copied the auth server's migration.
- `postgres` may insert into `auth.users` (A8). The patch notice says it
  has all permissions on `auth` entities.
- Supabase's pooler keeps `SET LOCAL` through transaction mode (A5).

## Out of scope

- The deploy pipeline, the environments, the secrets and the hosted image:
  spec 110.
- The production project. This spec touches staging only, and refuses a
  project with real users.
- Fixing a refuted assumption: its own spec, or an amendment of spec 105
  with a new migration.
- Supabase settings outside the database (the Data API toggle, sign-up,
  signing keys): spec 110's runbook.
- Creating any account or project. Akin does that.

## Hosting budget (2026-10-11)

Akin decided in chat on 2026-10-11 that the hosted deployment targets
under 5 USD per month, with ADR-013's topology kept and Vercel on its
free Hobby plan. Spec 110's "Hosting budget (2026-10-11)" holds the
prices and the arithmetic. To meet it, the API's one Fly machine stops
when idle, spec 106 keeps it awake while a run is active and records a
run cut off by a stop, and spec 108's scheduler is a Fly scheduled
machine that runs one tick per hourly start. Each of those rests on Fly
behavior that Fly's pages, read 2026-10-11, do not state. This section
checks it before those specs are built.

### Who runs what

Akin creates a throwaway Fly app in `ams` (Amsterdam, the region of spec
110's "Region (2026-10-11)"), called `harrier-probe` in the
documents, in the organization that will hold staging and production,
and deletes it after the run. The agent creates no account and no app.
The probes run no harrier code: none of specs 106, 108 or 110 is built
when this spec runs. The probe image is the operator's choice, holding a
shell, a small HTTP server and an HTTP client. Its commands and their
output go in the pull request, with every token, organization id and
hostname that is not the probe's removed. No file is added to the
repository for them.

The harrier half of each check (spec 106's keepalive and its
`run_records`, spec 108's one tick per start) is proven by those specs'
own tests, and on staging by spec 110's first deploy that runs them.

### The Fly assumptions

| | Assumption | Checked by |
|---|---|---|
| F1 | A machine with `auto_stop_machines = "stop"`, `auto_start_machines = true` and `min_machines_running = 0` stays started while a process on it sends `GET` to the app's public hostname at a fixed interval with no other traffic, and stops after the requests end | the probe app with those settings and one machine; the probe process beats for a period, then stops; `fly machine status` and the logs give the times of the last beat, the stop and the next cold start. Repeated with one request held open instead of beats. The interval used, and which variant kept the machine up, are recorded |
| F2 | An auto-stop sends the app's kill signal and waits before the machine stops, and a machine started again has its root file system reset from the image | the probe logs the signal it receives and the time to its stop; a file written to the root file system before the stop is absent after the next start |
| F3 | A machine created with `fly machine run --schedule hourly --restart no` fires about hourly, stays stopped between ticks, is not started a second time while a tick runs, and keeps its schedule through `fly machine update` | a scheduled probe that logs its start time and exits, observed for at least one day; one tick made to run past the next hourly start; one `fly machine update` to another image tag mid-way. The observed starts and their spread are recorded |
| F4 | Production's projected monthly cost under the expected usage is under 5 USD, and an app with no running machine, no volume and no dedicated IPv4 bills nothing beyond its machines' root file systems | the organization's billed usage after F1 to F3, read from Fly's billing; the projection uses the published rates (spec 110) and the expected usage Akin states in the pull request (hours a day the API is in use, runs a day and their length, ticks a month and their length), with the arithmetic shown |
| F5 | 512MB holds the API with Chromium rendering a PDF | only if spec 110's 512MB alternative is taken; it needs the hosted image, so it is checked by spec 110's first staging deploy, not here |

A refuted F1 sends spec 106 to its Open decision 9's alternatives. A
refuted F2 changes spec 106's "A run cut off by a stop" step 3 and leaves
step 4 as the only path. A refuted F3 reopens spec 108's Open decision
8. A refuted F4 goes back to Akin with the line that carries the cost.

### Acceptance criteria (hosting budget)

- [ ] The pull request records F1 to F4, each confirmed or refuted with
      the date, the commands and the observed values, and no token,
      organization id or private hostname.
- [ ] F1's record gives the beat interval used, the time from the last
      beat to the stop, and the cold start time on the next request.
- [ ] F4 shows its projection's arithmetic from the published rates and
      the billed usage, and states whether production fits under 5 USD.
- [ ] The probe app is deleted after the run, and the pull request shows
      it.
- [ ] "Amendment (staging findings)" marks F1 to F4 as it marks A1 to
      A11.

### Honest limitations (hosting budget)

- A probe shows Fly's behavior on one date in one region. Fly may change
  its proxy or scheduling; nothing notices until a re-run.
- The expected usage behind F4 is Akin's statement, not a measurement.
  Spec 110's monthly cost check is what measures it once production runs.
- Supabase's cost is not part of F4: spec 110's budget section records
  it as a gap.

### Proof / origin (hosting budget)

- Fly, Autostop/autostart: stops on excess capacity by `soft_limit`, the
  stop loop "runs every few minutes", nothing on open connections or
  background work. Read 2026-10-11.
  https://docs.fly.io/launch/autostop-autostart
- Fly, `fly machine run`: `--schedule` fuzzy cycles, scheduled Machines
  not startable by command, restart policies. Read 2026-10-11.
  https://docs.fly.io/machines/flyctl/fly-machine-run
- Fly, Pricing: no free tier for new organizations; machine and root file
  system rates. Read 2026-10-11. https://docs.fly.io/about/pricing

## Amendment (staging findings)

Run on 2026-10-11 against the staging project Akin created, empty before
the run, on Postgres 17.6 (`SELECT current_setting('server_version')`:
`17.6`). Every connection went through the session pooler or the
transaction pooler: the direct database host is IPv6 only and was not
reachable from the machine that ran it. The project's URLs, its
reference and every password stayed in the operator's shell and appear
nowhere below.

### Who ran what

This differs from Behavior's "Who runs what", on Akin's instruction for
this run: the agent ran the migration and the module, through a wrapper
that exports `HARRIER_SUPABASE_TEST_URL` (the `postgres` role through the
session pooler) without printing it. Nobody used the SQL editor.

- The migration, from `services/api`:
  `HARRIER_DATABASE_URL=<postgres through the session pooler> uv run --group postgres harrier store migrate`
  printed `postgres 0 -> 14` and exited 0. 14 is `target_version()`.
- The probe role was made by the run's setup step, not by hand:
  `CREATE ROLE harrier_api_probe LOGIN NOINHERIT PASSWORD '<verifier>'`,
  as `postgres`, with a password generated in memory and sent only as a
  SCRAM verifier computed on the client, so no statement or log holds it.
  Its pooler URL (user `harrier_api_probe.<project reference>`, the
  pooler's form for a custom role) was built in memory from the migrator's
  and exported to pytest as `HARRIER_SUPABASE_API_TEST_URL`. After the run
  its pooled sessions were ended and the role dropped (`probe role
  dropped: True`). Whether a later run makes the probe the same way or by
  hand, as Behavior says, is Akin's choice.
- The module, from `services/api`:
  `uv run --group postgres pytest -v -rfE --tb=short services/api/tests/test_supabase_project.py`
  with both URLs exported. Final run: 13 failed, 16 passed, 1 skipped
  (`services/api/tests/test_supabase_project.py::test_staging_mode_refuses_a_project_with_real_users`
  needs the local test server, which that shell did not name).

### The assumptions

| | Result | Evidence |
|---|---|---|
| A1 | confirmed | `services/api/tests/test_supabase_project.py::test_the_migrator_can_trigger_on_auth_users` passed: `pg_trigger` holds `harrier_new_owner` on `auth.users`, enabled (`O`). `auth.users` is owned by `supabase_auth_admin`; its ACL gives `postgres` `ar*wdDxtm` (INSERT, SELECT with grant option, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER, MAINTAIN). |
| A2 | **refuted** | `services/api/tests/test_supabase_project.py::test_the_tenant_role_reaches_auth_uid` failed: `(USAGE on auth, EXECUTE on auth.uid()) = (False, True)`. Schema `auth`'s ACL gives `postgres` `U/supabase_admin`, usage without the grant option, so migration 10's `GRANT USAGE ON SCHEMA auth TO harrier_tenant` grants nothing; Postgres warns and the migration completes. `EXECUTE` holds only because `auth.uid()`'s ACL gives it to `PUBLIC` (`=X/supabase_auth_admin`); migration 10's grant of it is a no-op too. See "What A2 breaks". |
| A3 | confirmed, its test fails on A2 | The deployed body (`prosrc`) equals the shim's (`services/api/tests/pg_support.py:136-141`) once whitespace and case are ignored: the old single claim, then `request.jwt.claims ->> 'sub'`, as uuid. As `authenticated`, with only `request.jwt.claims` set, `auth.uid()` returned its `sub`. The same call as `harrier_tenant` answered `42501 permission denied for schema auth`, so `services/api/tests/test_supabase_project.py::test_auth_uid_reads_the_claims_sub` fails until A2 is fixed. |
| A4 | confirmed | `services/api/tests/test_supabase_project.py::test_postgres_bypasses_but_is_not_superuser` passed: `rolsuper` false, `rolbypassrls` true, `rolcreaterole` true. Also found: `rolcreatedb`, `rolreplication` and `rolinherit` true; a member of `anon`, `authenticated`, `service_role` and `authenticator` with the admin option, INHERIT and SET. |
| A5 | confirmed | `services/api/tests/test_supabase_project.py::test_the_pooler_keeps_a_custom_role_and_set_local` passed. As `harrier_api_probe` through the session pooler (5432) and the transaction pooler (6543), with `prepare_threshold=None`: `current_user` is `harrier_api_probe`; inside a transaction, after `SET LOCAL ROLE harrier_tenant` and the local claims, the read-back is `('harrier_tenant', <sub>)`; after commit `current_user` is `harrier_api_probe` again. An earlier run failed here for a reason outside A5 (finding N3). |
| A6 | confirmed | `services/api/tests/test_owner_policy.py::test_an_owner_files_a_job_with_the_defaults` passed in staging mode: as `harrier_tenant`, `INSERT INTO jobs DEFAULT VALUES` returned the owner and track 1. The policy, the `owner_id` default and the foreign key reach `auth.uid()` and `auth.users` without schema usage. |
| A7 | confirmed | `services/api/tests/test_supabase_project.py::test_unqualified_names_resolve_to_public_for_both_roles` passed. `postgres`: `current_schemas(true)` = `{pg_catalog,public,extensions}`. `harrier_api_probe` as `harrier_tenant`, on both pooler ports: `{pg_catalog,public}`. `to_regclass('jobs')` is `public.jobs` in both. `rolconfig`: `postgres` has `search_path="$user", public, extensions`; `harrier_api_probe` and `harrier_tenant` have none. |
| A8 | confirmed | `services/api/tests/test_supabase_project.py::test_the_migrator_can_create_an_auth_user` passed: `postgres` inserted a random id into `auth.users`, and the trigger gave it track 1. |
| A9 | confirmed, its test fails on N2 | No harrier table, column, sequence or function holds a grant to `anon`, `authenticated`, `service_role` or `PUBLIC`, and `postgres` holds no default privilege to them in `public` or global. `supabase_admin`'s defaults in `public` grant those roles everything, but only on objects `supabase_admin` creates. `services/api/tests/test_owner_policy.py::test_every_table_is_owned_or_declared` fails in staging mode, and every problem it names is about `rls_auto_enable()`, a Supabase function (N2). |
| A10 | confirmed | `services/api/tests/test_supabase_project.py::test_the_migrator_can_grant_the_tenant_role` passed: after a revoke, `GRANT harrier_tenant TO harrier_api_probe` as `postgres` succeeded, and `pg_auth_members` shows the membership with SET. |
| A11 | confirmed | `services/api/tests/test_supabase_project.py::test_the_store_migrates_to_the_target_as_a_non_superuser_migrator` passed: the store is at `target_version()` (14), migrated by `postgres`, not a superuser. `postgres`'s `pg_default_acl` after the run: global functions `{postgres=X/postgres}`; in `public`, tables, sequences and functions granted to `postgres` alone. A function made in `public` in a transaction that is rolled back is executable by none of `PUBLIC`, `anon`, `authenticated` and `service_role`. `postgres`'s defaults in schema `storage` still grant those roles; A11 does not cover that schema. |

### What A2 breaks

Every statement that names `auth.uid()` while the session is
`harrier_tenant` is refused with `42501 permission denied for schema
auth`. Schema usage is checked when a name is looked up, so the policy
and the column default, which hold the function by its id, still work
(A6). These do not:

- **Numbering a track.** `harrier_number_track()` (migration 14) is not a
  definer function and names `auth.uid()` in its body, which PL/pgSQL
  looks up as the session's role on first use. An owner cannot insert a
  track without an id, which is every track but track 1.
- A direct `SELECT auth.uid()` as the tenant role, as A3's test and
  `services/api/tests/test_owner_policy.py::test_an_insert_without_an_owner_is_refused`
  run.

Spec 104's read-back reads `request.jwt.claims` directly and is not
affected (A5).

**Owner of the fix: spec 105**, by a further amendment with a new
migration, before spec 112 is built, as Scope item 6 says. `postgres`
holds no grant option on `auth`, so that amendment cannot grant the usage;
it must reach the owner without naming `auth.uid()` as the tenant role.
This spec's acceptance waits on it. The fix has a CI proof ready: with the
shim made faithful on this one grant (below), the suite fails as the
project does.

### The isolation tests in staging mode

Run as `postgres` against the migrated project, after `open_staging`:

- Passed: `services/api/tests/test_owner_policy.py::test_an_owner_files_a_job_with_the_defaults`,
  and `services/api/tests/test_owner_policy.py::test_uniqueness_is_per_owner`
  for every key but `tracks_owner_id_slug_key`.
- Failed on A2 at their first tenant insert into `tracks` without an id
  (most of them inside `fill`), or at `SELECT auth.uid()` as the tenant:
  `test_owner_a_reads_nothing_of_owner_b`,
  `test_owner_a_cannot_write_as_owner_b`,
  `test_an_insert_without_an_owner_is_refused`,
  `test_the_data_api_role_reaches_no_harrier_table`,
  `test_uniqueness_is_per_owner[tracks_owner_id_slug_key]`,
  `test_references_stay_inside_one_owner`,
  `test_every_owner_starts_with_track_one`,
  `test_a_bypassrls_role_sees_every_owner`, and both cases of
  `test_truncate_cannot_empty_events_or_tracks`, whose TRUNCATE refusal was
  never reached.
- Failed on N2: `test_every_table_is_owned_or_declared`.

So the acceptance criterion "The eleven `test_owner_policy.py` tests ...
pass in staging mode" is not met. It is re-run after spec 105's fix for A2
and its declaration for N2.

### Other findings

- **N1. `postgres` cannot SET ROLE to the tenant role it created.** It
  holds `harrier_tenant` with the admin option only (INHERIT false, SET
  false, grantor `supabase_admin`), because Supabase leaves
  `createrole_self_grant` empty. `SET LOCAL ROLE harrier_tenant` as
  `postgres` answered `42501 permission denied to set role
  "harrier_tenant"`. Staging mode grants `harrier_tenant TO postgres WITH
  INHERIT FALSE, SET TRUE` before any test (`open_staging` in
  `services/api/tests/pg_support.py`), and that grant stays on the
  project. A request does not depend on it: the API's own login role gets
  SET from A10's grant, which defaults to SET. Spec 110's runbook grants
  the API role that way.
- **N2. Supabase puts a definer function in `public`.**
  `rls_auto_enable()` is owned by `postgres`, `SECURITY DEFINER` with
  `search_path=pg_catalog`, executable by `PUBLIC`, `anon`,
  `authenticated` and `service_role`, returns `event_trigger`, and is
  fired by the event trigger `ensure_rls` on `CREATE TABLE`. It cannot be
  called directly (as `anon`: `0A000 cannot display a value of type
  event_trigger`). Spec 105's catalog rules name it as a definer function
  in `public`, without an empty `search_path`, executable by every role.
  **Owner of the fix: spec 105**, in the same amendment as A2: the catalog
  declares Supabase's own objects in `public`, or skips event trigger
  functions, with its own acceptance criterion. It is not harrier's object
  and is left alone.
- **N3. The pooler keeps a dropped role's connections.** The first run
  dropped `harrier_api_probe` at its end; the next made it again under the
  same name. The pooler then handed the new role an idle server
  connection still logged in as the dropped one:
  `psycopg.errors.UndefinedObject: invalid role OID` and `permission
  denied to set role "harrier_tenant"`. `pg_stat_activity` showed two idle
  `Supavisor` backends whose `usesysid` no longer existed;
  `pg_terminate_backend` on those two, as `postgres`, cleared it, and the
  clean run passed A5. The run now ends the probe's sessions before it
  drops the role. **Owner: spec 110**, whose runbook creates and rotates
  the API's login role: dropping and remaking a login role needs its
  pooled sessions ended first.

### CI migrates as a non-superuser

`services/api/tests/pg_support.py` now makes `harrier_migrator` (`LOGIN
NOSUPERUSER INHERIT CREATEROLE CREATEDB REPLICATION BYPASSRLS`, A4's
attributes), owner of every test database, and `fresh_database` hands
every test its URL, so every Postgres test migrates and connects as it
(`services/api/tests/test_owner_policy.py::test_ci_migrates_as_a_non_superuser`,
and the CI form of A11,
`services/api/tests/test_owner_policy.py::test_the_store_migrates_to_the_target_as_a_non_superuser_migrator`).
The superuser creates and drops the databases, installs the shim, and
runs the three tests that give the tenant role `SUPERUSER`, which only a
superuser may. The shim gives the migrator what A1, A2, A4 and A8 found
`postgres` holds: the auth.users privileges above, nothing on
`auth.uid()` beyond `PUBLIC`'s, and membership of `anon`, `authenticated`
and `service_role`. Choices this change made that the text above left
open:

- **One recorded difference from the project.** The migrator holds USAGE
  on `auth` with the grant option, which `postgres` lacks (A2). Without
  it CI fails as the project does: with the grant option removed, 15
  tests in `services/api/tests/test_owner_policy.py` fail with
  `permission denied for schema auth`. The shim keeps it so the suite
  stays green until spec 105's fix for A2, whose change removes it.
- **SET on the tenant role.** The migrator has `createrole_self_grant =
  'set'`, or is granted the tenant role with the admin option and SET when
  another role made it first, so tests can act as an owner. This is N1's
  grant, made in the shim instead of by staging mode.
- **Tests adjusted to run without a superuser, or on a shared project.**
  `services/api/tests/test_owner_policy.py::test_a_bypassrls_role_sees_every_owner`
  asserts both owners are among those a bypassing role sees, since a
  shared project holds earlier runs' owners;
  `services/api/tests/test_owner_policy.py::test_an_insert_without_an_owner_is_refused`
  names the refused role in its assertion; the catalog breach that changes
  a table's owner first grants the new owner `CREATE` on `public`, which a
  non-superuser needs.
- **Staging mode is a module, not a flag.** `services/api/tests/test_supabase_project.py`
  collects the listed `test_owner_policy.py` test functions under its own
  `store` and `store_url` fixtures, which yield the project after
  `open_staging`. `open_staging` refuses a project whose `auth.users`
  holds a user with an email, then one not at `target_version()`, before
  it writes
  (`services/api/tests/test_supabase_project.py::test_staging_mode_refuses_a_project_with_real_users`).
  Missing either URL skips the module naming the variable, in CI too
  (`services/api/tests/test_supabase_project.py::test_supabase_checks_skip_without_a_project`).

### What the run left on the project

Schema version 14 and harrier's objects; the synthetic owners the tests
made, with no email, and their tracks and rows (Behavior: "What the run
leaves behind"); `harrier_tenant`, with N1's grant to `postgres`. The
probe role and its sessions are gone. Nothing Supabase owns was changed.

### Hosting budget

F1 to F4 were not run: they need a paid Fly app, which Akin has not
approved. They stay unchecked, and so do their criteria.

### Acceptance status

Met: the migration output above; the module with one test per assumption
and the two planned tests; every Postgres test migrating as
`harrier_migrator` (locally, `just check`; CI shows it on the pull
request); the CI form of A11; `docs/architecture.md`'s paragraph; ruff
and pyright. Not met: the eleven isolation tests in staging mode (A2,
N2), and therefore this spec's acceptance as a whole, which waits on
spec 105's amendment; the Fly checks. The criteria above now cite the
tests this change wrote instead of marking them planned, as the spec
citation check requires once they exist. The A11 test's name in the
criteria is left to the pull request that renames it there.

### Line numbers, refreshed (D9)

On `main` at `d6b551f` with this change: the shim is
`services/api/tests/pg_support.py:122-186` and is installed by `:268-284`;
the shim's `auth.uid()` body is `:136-141`; `new_owner` is `:287-292`;
the per-test database is `fresh_database`, `:398-427`. In
`services/api/src/harrier/tracker/schema.py` the trigger on `auth.users`
is `:908-911` and the grants on `auth` are `:959-960`.

### Re-run after spec 105's amendment (2026-10-11)

"The isolation tests in staging mode" says they are re-run after spec
105's fix for A2 and its declaration for N2. Both landed as spec 105's
"Amendment (2026-10-11): owner identity without schema auth" (migration
16 and `SUPABASE_FUNCTIONS_IN_PUBLIC`), and the re-run is recorded in full
there, under "Implementation record (2026-10-11)". In short, on the same
project, on Postgres 17.6:

- `harrier store migrate` as `postgres` printed `postgres 14 -> 16` and
  exited 0.
- The module, run as the first run did it: 2 failed, 29 passed. The
  eleven `test_owner_policy.py` tests listed under Behavior pass in staging
  mode, `services/api/tests/test_owner_policy.py::test_every_table_is_owned_or_declared`
  among them with `rls_auto_enable()` declared.
- The two failures are this spec's A2 and A3 tests,
  `services/api/tests/test_supabase_project.py::test_the_tenant_role_reaches_auth_uid`
  and `services/api/tests/test_supabase_project.py::test_auth_uid_reads_the_claims_sub`.
  They still assert that the tenant role reaches `auth.uid()`, which spec
  105 no longer needs; restating or retiring them is the follow-up spec
  105's amendment assigns this spec.
- N3 again: after the probe role was dropped, two idle pooler sessions of
  it were left, and were ended by hand.

## Amendment (2026-10-11): A2 and A3 after migration 16

Spec 105's migration 16 (PR #260) moved owner identity off `auth.uid()`
to `public.harrier_owner_id()` and revoked the tenant role's grants on
schema `auth`. The re-run above failed only this spec's A2 and A3 tests,
which still assert that the tenant role reaches `auth.uid()`. Migration
16 removes that by design, so the tests fail on staging for a reason that
is not a finding. This section restates both assumptions so the module
asserts what spec 105 now rests on, and keeps what each still tells a
re-run. It is the follow-up spec 105's amendment assigns this spec
("Specs this touches, and follow-ups").

### A2 and A3, restated

These two rows replace A2 and A3 in the table under "The assumptions".
The other rows stand.

| | Assumption | Checked by |
|---|---|---|
| A2 | Harrier needs nothing in `auth`. The migrating role holds USAGE on `auth` without the grant option, as the shim copies it; `harrier_tenant` holds no USAGE on `auth`; and as `harrier_tenant`, with claims set, `SELECT auth.uid()` is refused with `42501 permission denied for schema auth` | `has_schema_privilege` for `current_user` on `auth` with `USAGE` (true) and with `USAGE WITH GRANT OPTION` (false), and for `harrier_tenant` with `USAGE` (false); the refused call, in a savepoint |
| A3 | `auth.uid()` reads `sub` from `request.jwt.claims`, and `harrier_owner_id()` reads the same `sub` as the tenant role | the deployed `auth.uid()` body equals the shim's once whitespace and case are ignored, as before; as `authenticated`, with only `request.jwt.claims` set, `auth.uid()` returns its `sub`; as `harrier_tenant`, with the same claims, `public.harrier_owner_id()` returns it |

Why each half stays:

- **A2's migrating-role half** is the grant the CI shim copies
  (`SHIM_STATEMENTS` in `services/api/tests/pg_support.py`). Spec 105's
  Honest limitations say CI does not notice a change to it until a re-run
  of this module; this assertion is what notices. The tenant half checks
  migration 16's end state on the project, where its step 6 revokes warn
  and do nothing because migration 10's grants never took effect.
- **A3's body comparison** is how spec 105's Honest limitations say a
  Supabase change to `auth.uid()` is noticed, since `harrier_owner_id()`
  copies half of its body. The call as `authenticated`, which holds USAGE
  on `auth`, keeps the comparison tied to the live function. The tenant
  half moves from `auth.uid()` to `harrier_owner_id()`, the function
  spec 105's policy and defaults call.

A refuted A2 now means harrier's tenant role reaches `auth` on the
project, or the shim's copy of the migrating role's grant is out of date.
A refuted A3 means the owner a request is policed as differs from the
`sub` Supabase would give it. Either is a finding with a spec that owns
its fix, as Failure modes says.

### The tests

- `test_the_tenant_role_reaches_auth_uid` is retired: it asserts the
  opposite of A2 as restated. Its replacement is planned
  test_harrier_needs_nothing_in_auth_on_the_project, in the same module.
  The change that writes it removes the old test, and in this spec's
  Acceptance criteria and in row A2 of "Amendment (staging findings)" it
  writes the old name outside a code span followed by "(retired
  2026-10-11)", so the record of the first runs stays readable and the
  citation check, which skips a removed test named in prose, still
  passes.
- `test_auth_uid_reads_the_claims_sub` keeps its name and is restated: it
  calls `auth.uid()` as `authenticated` only, and calls
  `public.harrier_owner_id()` as `harrier_tenant`.

### Acceptance criteria (A2 and A3 after migration 16)

- [ ] The module's A2 test asserts the three values in the restated row,
      and its failure message names each observed value
      (planned test_harrier_needs_nothing_in_auth_on_the_project).
- [ ] The A3 test asserts the restated row: the body comparison, `auth.uid()`
      as `authenticated`, and `public.harrier_owner_id()` as
      `harrier_tenant`, and calls `auth.uid()` as no other role
      (`services/api/tests/test_supabase_project.py::test_auth_uid_reads_the_claims_sub`).
- [ ] Both tests were run against a local database built by the CI shim
      and migrated to `target_version()`, named by the module's URL
      variables, and pass there; each was then run against that database
      broken one way and failed on the assertion named: the migrator
      given the grant option on `auth` back, and USAGE on `auth` granted
      to `harrier_tenant` (A2); `harrier_owner_id()` replaced to return
      another UUID (A3). The pull request records each run.
- [ ] A re-run of this module on the staging project, as the re-run above
      did it, passes both, and the pull request records the output with no
      project reference, host, URL or password.
- [ ] `test_the_tenant_role_reaches_auth_uid` no longer exists, and every
      citation of it in this spec is rewritten as "The tests" says.
