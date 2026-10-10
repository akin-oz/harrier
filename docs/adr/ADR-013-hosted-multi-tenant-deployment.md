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
machine, so every tenant's requests and runs must reach the machine that
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
shape. Auth issues signed JWTs the API can verify. Storage applies the same
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
2. **Domain SQL never names a tenant.** ADR-012 point 2 holds unchanged.
   The domain filters by track. The database filters by owner, from the
   session. No domain function takes an owner parameter.
3. **Each request runs as its user.** The API verifies the Supabase JWT
   (signature against the project's published keys, expiry, audience) and
   opens the request's transaction as the `authenticated` role with that
   token's claims set, so `auth.uid()` resolves inside the database. A
   request without a valid token is answered 401 and opens no transaction.
   The `service_role` key is used only by migrations and by operator
   commands that are not reachable from any HTTP route.
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
  written once and must produce valid DDL for both, and CI runs the tracker
  tests against both.
- The local-first and privacy invariants are amended (spec 102) to name
  the hosted deployment as the one place these vendors appear.
- Spec 035's trusted-host and no-auth assumptions describe the local API.
  The hosted API is reachable from the internet and is authenticated; spec
  104 states which of spec 035's guards carry over.
- Backups of the hosted store are Supabase's, plus whatever spec 110 adds.
  `just backup` stays local.
- A tenant's data can be exported and deleted on request; spec 111 owns it.

## Honest limitations

- Nothing hosted exists after this ADR. It is a decision and a sequence.
- Row-level security protects rows only when the connection runs as
  `authenticated`. A code path that uses the service role bypasses it. The
  guard is decision 3's rule plus a test in spec 105, not the database.
- Google's restricted Gmail scopes need app verification and a security
  assessment before external users can grant them. Hosted Gmail watch may
  not be available at first; spec 109 decides.
- Running LinkedIn and academic searches through Apify on tenants' behalf
  depends on each tenant's own Apify account and the terms they accept.
