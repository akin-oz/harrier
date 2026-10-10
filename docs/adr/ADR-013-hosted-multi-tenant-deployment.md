# ADR-013: Hosted multi-tenant deployment on Supabase, Fly.io and Vercel

- Status: accepted
- Date: 2026-10-10
- Extends: ADR-012 (its hosted half), ADR-009 (its point 3)
- Preserves: ADR-003, ADR-010, ADR-011 for the local product, unchanged
- Source: specs/102-hosted-multi-tenant-deployment.md

## Context

The local product is one person, one data directory, one SQLite file, one
kernel. ADR-012 chose a store boundary for tenants locally (a directory per
person) and said that model "does not serve many people from one service".
A hosted service needs a store that many people share, a way to know which
person a request belongs to, and a guarantee that the database refuses
cross-person reads even when application code forgets.

## Options weighed

**(a) One SQLite file per tenant on Fly volumes.** ADR-012's local model,
hosted. Isolation stays the file system. A Fly volume attaches to one
machine (https://fly.io/docs/volumes/overview/, read 2026-10-10), so every tenant's requests and runs must reach the machine that
holds their file; scaling is sharding by hand; backup is per volume; auth
still has to be built. Rejected: it moves ADR-011's single-owner problem
onto a network and solves none of the hosted questions.

**(b) Self-managed Postgres on Fly, own authentication.** Row-level policy
is available. Auth, password reset, email verification, OAuth sign-in and
key rotation are all ours to build and to get wrong, and the database is
ours to back up and upgrade. Rejected for the first hosted version: the
work is real and is not what the product is about.

**(c) Supabase Postgres with Supabase Auth and Storage; API on Fly; SPA on
Vercel.** Row-level policy keyed by `auth.uid()` is the platform's normal
shape (https://supabase.com/docs/guides/database/postgres/row-level-security,
read 2026-10-10). Auth issues signed JWTs the API can verify. Storage applies the same
policy model to files. Fly runs a long-lived container, which the run
manager (ADR-004), Chromium for PDF rendering and subprocess runs need and
which a serverless function does not offer. Vercel serves the static SPA
build. Costs: three vendors, two database dialects (SQLite locally,
Postgres hosted), and a JWT boundary the local product never had. Chosen.

## Decision

1. **The hosted store is Supabase Postgres.** Every table that holds
   personal data (today `jobs`, `contacts`, `profile_documents`,
   `user_config`, `job_runs`, `job_events`, `tracks`) gains in the hosted
   schema only an `owner_id uuid not null` referencing `auth.users`,
   defaulting to `auth.uid()`. Row-level security is enabled and forced on
   each, with one policy: `owner_id = auth.uid()` for every command.

   **Amended (spec 105).** This named the column and the policy as if they
   were the whole change. The baseline was written for one person, and its
   keys assumed it. Migration 10 in
   `services/api/src/harrier/tracker/schema.py` is hosted only: SQLite
   records the version and changes nothing, so the local schema still
   names no tenant (ADR-012 point 3). On Postgres:

   - The column is `owner_id uuid NOT NULL DEFAULT auth.uid() REFERENCES
     auth.users (id)`, with no `ON DELETE` action, so deleting a user who
     still owns rows is refused. Spec 111 decides deletion.
   - The policy is `owner_only`, `FOR ALL TO harrier_tenant`, with `USING`
     and `WITH CHECK` both `owner_id = (SELECT auth.uid())`. The wrapped
     call runs once per statement. Without claims it is null and matches
     nothing, so the session reads and stores nothing.
   - Every unique key, and `job_runs`' primary key, leads with `owner_id`.
     Two owners may hold the same URL, slug, config kind, document name or
     run name, and a refusal no longer tells one owner what another holds.
   - Track ids are numbered per owner, so every owner has a track 1, created
     by a trigger on `auth.users`, and the domain's default track keeps its
     meaning without naming a tenant.
   - `jobs` references `tracks (owner_id, id)` and `job_events` references
     `jobs (owner_id, id)`, so a row cannot point at another owner's row.
   - `schema_version` has row security enabled and forced too, with a
     read-only policy, so no table is an exception.
   - The policed role is `harrier_tenant`, a harrier-owned role with no
     login, which migration 10 creates. The migration revokes every
     privilege from `PUBLIC`, `anon`, `authenticated` and `service_role`
     and grants `harrier_tenant` only what the domain uses. Supabase's Data
     API switches to `authenticated`, so it reaches no harrier table, and
     `harrier.tracker` stays the one write path. It relies on no Supabase
     default.

   `FORCE` does not bind a role with `BYPASSRLS`, and on Supabase that
   includes `postgres` and `service_role`. The guard against a bypassing
   connection is decision 3, enforced by spec 104.
   `services/api/tests/test_owner_policy.py::test_every_table_is_owned_or_declared`
   holds every table in the schema to this shape, and
   `services/api/tests/test_owner_policy.py::test_the_data_api_role_reaches_no_harrier_table`
   proves the Data API's role is refused.
2. **Domain SQL never names a tenant.** ADR-012 point 2 holds unchanged.
   The domain filters by track. The database filters by owner, from the
   session. No domain function takes an owner parameter.
3. **Each request runs as its user.** The API verifies the Supabase JWT
   (signature against the project's signing keys, expiry, audience; which
   keys and algorithms is spec 104's, since Supabase publishes asymmetric
   keys as a key set and older projects sign with a shared secret:
   https://supabase.com/docs/guides/auth/signing-keys) and
   opens the request's transaction as `harrier_tenant` with that token's
   claims set, so `auth.uid()` resolves inside the database. A
   request without a valid token is answered 401 and opens no transaction.
   Migrations run as the schema-owner login `HARRIER_DATABASE_URL` names
   (`harrier store migrate`, spec 103). The service role is used only by
   operator commands that are not reachable from any HTTP route, each
   granting itself what it needs in its own migration (spec 105).

   Amended (spec 105). This said the request runs as `authenticated`, the
   role Supabase's Data API also uses. Granting harrier's tables to it
   would have opened a second write path past `harrier.tracker`, closed
   only by a deploy setting. The request now switches to `harrier_tenant`,
   which only harrier's own login role can reach.
4. **Tenant resolution at every boundary.**
   - HTTP request: the verified JWT's `sub`.
   - Run started from a request (ADR-004): the run records the owner at
     start, and its own database session runs as that owner. A run with no
     owner is refused before it starts.
   - Scheduled run: the scheduler lists owners with due schedules using the
     service role, then starts each owner's run as that owner. It never
     executes domain code as the service role.
   - CLI: the hosted deployment has no tenant CLI. Operator commands run as
     the service role and are named as such.
5. **One write path, per dialect.** `harrier.tracker` stays the only writer
   (ADR-003, ADR-012 point 4). The schema keeps one definition in
   `services/api/src/harrier/tracker/schema.py`: each migration declares
   its SQLite and Postgres statements in that module, held to the same
   versions, Postgres history begins with a baseline at the version the
   local schema had when the hosted store was introduced, and a parity
   test holds the two to the same tables, columns, defaults and refusals.
   The hosted-only columns and policies are part of that definition,
   marked hosted. Migrations stay harrier's own runner (spec 090) with a
   Postgres dialect. Supabase CLI migrations are not used, because they
   would be a second schema definition.

   **Amended (spec 103).** This said SQLite and Postgres DDL "are produced"
   from one definition. Nine migrations of SQLite history cannot be
   regenerated without rewriting history, and a generator over triggers
   and dialect functions is a second language to maintain. Declaring both
   dialects in one module, with a test that fails when they disagree, is
   the property the sentence was for.

   Spec 103 planned both dialects in one entry of `MIGRATIONS` and the
   Postgres runner as a branch of `harrier.db._apply_schema`. It was built
   differently, and the property is the same. The Postgres statements sit
   in a parallel list, `POSTGRES_MIGRATIONS`, in the same module.
   `undeclared_dialects` requires every version after the baseline in both
   lists with at least one statement
   (`services/api/tests/test_postgres_store.py::test_every_new_migration_declares_both_dialects`).
   The Postgres runner is `harrier.pgstore.apply_postgres_migrations`, so
   the SQLite open path never imports the driver. Spec 103's amendment says
   why.
6. **Files live in Supabase Storage.** Generated artifacts and run state
   that are files locally (resumes, letters, answers, drafts, summaries,
   seen-state, trained scoring models) are objects in a private bucket
   under a path prefixed by the owner's id, with a storage policy matching
   decision 1.
7. **Topology.** Vercel serves the SPA build only; it holds no secret but
   the Supabase URL, the anon key and the API URL. Fly runs the API image
   (FastAPI, run manager, Chromium, poppler) in one region. Supabase runs
   the database, auth and storage in a region in the same jurisdiction.
8. **Providers in the hosted deployment.** Only API LLM providers
   (`openai-api`, `anthropic-api`) are available hosted. The CLI providers
   authenticate as one person's subscription and are local-only. Every
   tenant credential (AI key, Apify token, Telegram bot, Hunter key, Gmail
   OAuth token) is stored per owner, encrypted at rest, and reaches only
   that owner's runs. A run's subprocess environment carries its owner's
   credentials and no one else's.
9. **The local product does not change.** SQLite per data directory,
   ADR-010's container, ADR-011's single kernel, launchd and the CLI
   providers stay as they are. Nothing hosted-only is required to run the
   demo or the local tool from a fresh clone.

## Consequences

- Two database dialects are a permanent cost. Every schema change is
  written in both dialects in `services/api/src/harrier/tracker/schema.py`,
  held together by the parity test (`services/api/tests/test_dialect_parity.py`,
  spec 103). CI runs the store and parity tests against Postgres; the
  tracker domain tests follow with spec 112.
- The local-first and privacy invariants are amended (spec 102) to name
  the hosted deployment as the one place these vendors appear.
- Spec 035's trusted-host and no-auth assumptions describe the local API.
  The hosted API is reachable from the internet and is authenticated; spec
  104 states which of spec 035's guards carry over.
- Backups of the hosted store are Supabase's, plus whatever spec 110 adds.
  `just backup` stays local.
- A tenant's data can be exported and deleted on request; spec 111 owns it.

## Honest limitations

- Nothing hosted existed when this ADR was accepted. Spec 103 has since
  added the Postgres store and `harrier store`; nothing serves a tenant yet.
- Row-level security protects rows only when the connection runs as
  `harrier_tenant`. A code path that uses the service role bypasses it. The
  guard is decision 3's rule plus a test in spec 104, not the database.
- Google's restricted Gmail scopes need app verification and, when the
  data is stored on servers, a security assessment before external users
  can grant them (https://developers.google.com/workspace/gmail/api/auth/scopes,
  read 2026-10-10). Hosted Gmail watch may
  not be available at first; spec 109 decides.
- Running LinkedIn and academic searches through Apify on tenants' behalf
  depends on each tenant's own Apify account and the terms they accept.
