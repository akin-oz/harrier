---
spec: 110
title: Deploy pipeline and environments
status: proposed
approved: no
milestone: M10
depends: [103, 104, 105]
---

# Spec 110: Deploy pipeline and environments

## Problem

No hosted environment exists. ADR-013 decision 7 names Fly, Vercel and
Supabase. Akin chose staging and production (spec 102, open decision 4) in
the EU (open decision 2).

The post-merge review of PRs #194 and #207 (2026-10-10) added these:

- **Spec 105 is proven only against a shim.** CI's Postgres is plain
  Postgres with Supabase's roles and `auth.uid()` imitated
  (`services/api/tests/pg_support.py`). Four assumptions are unchecked on
  a real Supabase project: the migrating role can create a trigger on
  `auth.users`; the deployed `auth.uid()` body reads `sub` from
  `request.jwt.claims`; the pooler accepts a custom login role; and the
  foreign key from `owner_id` to `auth.users` holds on insert as the
  policed role, `harrier_tenant`. Spec 105's Amendment hands this check
  two more: the migrating role may grant `harrier_tenant` usage on
  `auth`, and Supabase's `postgres` has `BYPASSRLS`. Akin took the
  review's recommendation on 2026-10-10: this spec's first slice, which
  migrates a real Supabase staging project to the latest version (10
  today, `services/api/src/harrier/tracker/schema.py:853`) and runs spec
  105's isolation tests (`services/api/tests/test_owner_policy.py`)
  there, lands before spec 112. (architecture 3)
- **Unqualified table names.** Domain SQL and migrations name tables
  without a schema, so the migrating role and the request role must
  resolve the same `search_path`. The first slice checks it. (data
  integrity 5)
- **New paths need a class first.** `.gitignore` ignores `.env` and
  `.env.*` (`.gitignore:2-3`), but `config/data-classification.json`
  lists only `.env` and `.env.local` (`:6-7`). Per-environment env files
  and any `supabase/` or Fly configuration paths are classified before
  they are added. (privacy 5)
- **Hosted logs leave the machine.** Fly collects the API's logs. This
  spec states the redaction and prompt-logging rules for hosted logs.
  (privacy 5)
- **`.env` reaches the container.** `docker-compose.yml:51-53` loads
  `.env` with `env_file`, so a hosted URL written there is a production
  credential in the local container's environment. `harrier store
  migrate` for a hosted store runs from a shell with the URL exported,
  not through `.env`. (privacy 5)
- **Tracebacks hold the URL.** A `StoreConnectionError` raised in
  `postgres_connect` (`services/api/src/harrier/pgstore.py:137-149`)
  carries that frame, whose locals include the URL and its password. Any
  hosted error reporter that records frame locals, as Sentry does, must
  scrub them or turn locals off. (test-integrity follow-up)

## Scope

Stub created by spec 102 to sequence the hosted build. It is refined into a
real spec, with behavior and failure modes, before approval is asked for.

## Acceptance criteria

Headline, to be expanded into checkable criteria when refined: Staging and
production each have a Supabase project, a Fly app and a Vercel environment
in the EU; migrations apply before a new API version serves; no secret
enters git. The deployed project's Data API does not expose the schema that
holds harrier's tables, checked at deploy, so a tenant cannot write past
`harrier.tracker` with their own token and the anon key (spec 105, open
decision 6). A first slice lands before spec 112: a real Supabase staging
project is migrated to the latest version and spec 105's isolation tests
pass there, with each assumption above recorded as confirmed or refuted,
the `search_path` of the migrating and request roles included. Every new
env file or deploy path is classified before it is added; hosted logs
follow written redaction and prompt-logging rules; hosted migrations take
the URL from the shell, not `.env`; and an error reporter never records
frame locals unscrubbed (post-merge review of PRs #194 and #207,
2026-10-10).

## Proof / origin

ADR-013 decision 7; spec 102 open decisions 2 and 4; spec 105, Amendment;
the post-merge review of PRs #194 and #207 (2026-10-10).

## Out of scope

Pricing and billing.
