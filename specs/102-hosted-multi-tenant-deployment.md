---
spec: 102
title: Decide the hosted multi-tenant deployment (ADR-013) and split its build
status: accepted
approved: yes
milestone: M10
depends: [035, 090, 091, 092]
---

# Spec 102: Decide the hosted multi-tenant deployment (ADR-013) and split its build

## Problem

Harrier runs for one person on one machine. The tracker is a SQLite file in
a data directory (ADR-003, ADR-012), one kernel owns it (ADR-011), launchd
runs the schedule (ADR-006), the API binds to `127.0.0.1` (ADR-010, spec
035), and generated artifacts are files under the checkout. A second person
can only use it by cloning the repo and running their own copy.

The product is meant to be offered to other people as a hosted service.
Three ADRs already point at that and stop short of deciding it:

- ADR-009 point 3 hands "auth, isolation, and hosting" to "a future
  multi-tenant ADR" (`docs/adr/ADR-009-user-configuration-and-tenancy.md`).
- ADR-012 point 2 fixes one constraint (in a hosted store, the tenant
  boundary is the database's own row-level policy, applied from the
  session, never a predicate the domain writes) and its Consequences list
  what ADR-013 owns: authentication, tenant resolution at every boundary,
  the row-level policy, migration tooling for its database, the amendment
  to the local-first invariant, and the addition to the closed list of
  cloud dependencies
  (`docs/adr/ADR-012-search-tracks-and-tenant-isolation.md`).
- Spec 091 says ADR-013 "should be written before any hosted spec"
  (`specs/091-every-tracker-row-belongs-to-a-search-track.md:490`), and
  spec 090 defers "migration tooling for a hosted store (ADR-013)"
  (`specs/090-migrations-apply-whole-or-not-at-all.md:243`).

Two product invariants block a hosted deployment as written today:

- **Local-first** allows no cloud dependency beyond Apify, the AI
  providers, Telegram, Gmail API and Hunter. A hosted database, host and
  CDN are none of those.
- **Privacy** says personal data "lives in the local database or local
  files". In a hosted service it lives in the tenant's hosted store.

Akin chose the stack on 2026-10-10: Supabase (Postgres, Auth, Storage),
Fly.io for the API and run workers, Vercel for the SPA. Building any of it
before the decision is written would let the first implementation spec
decide tenancy by accident, which is what ADR-012 was written to prevent.

## Scope

This spec changes documents only. It carries ADR-013's full text, amends
the two invariants it must amend, and sequences the build as stub specs.
No code, schema, workflow, classification entry or contract changes.

1. `docs/adr/ADR-013-hosted-multi-tenant-deployment.md`: the text in the
   ADR-013 section below, with status `accepted`.
2. `.ai/rules/product-invariants.md`: the Local-first bullet amended to the
   text under Behavior. `aie sync` regenerates `CLAUDE.md`, `AGENTS.md`
   and the generated rule files; nothing generated is hand-edited.
3. `.ai/rules/privacy.md`: the personal-data bullet amended to the text
   under Behavior, regenerated the same way.
4. `docs/adr/ADR-012-search-tracks-and-tenant-isolation.md`: status line
   becomes "accepted; hosted half decided by ADR-013". Its decision text
   is not changed.
5. `docs/adr/ADR-009-user-configuration-and-tenancy.md`: status line gains
   "hosting decided by ADR-013".
6. `docs/architecture.md`: a section "Hosted deployment (decided, not
   built)" that names ADR-013 and the follow-on specs, and says no hosted
   component exists yet.
7. Stub specs 103 to 111 (listed under Follow-on specs), each with
   `approved: no`, `status: proposed`, a Problem paragraph, and the
   acceptance headline given here. Each is refined into a real spec before
   its approval is asked for, as `specs/README.md` already requires of
   stubs.
8. `specs/README.md`: an M10 line listing 102 to 111.

## ADR-013: Hosted multi-tenant deployment on Supabase, Fly.io and Vercel

This section is the ADR's full text. The implementing branch writes it to
`docs/adr/ADR-013-hosted-multi-tenant-deployment.md`.

- Status: accepted
- Extends: ADR-012 (its hosted half), ADR-009 (its point 3)
- Preserves: ADR-003, ADR-010, ADR-011 for the local product, unchanged
- Source: specs/102-hosted-multi-tenant-deployment.md

### Context

The local product is one person, one data directory, one SQLite file, one
kernel. ADR-012 chose a store boundary for tenants locally (a directory per
person) and said that model "does not serve many people from one service".
A hosted service needs a store that many people share, a way to know which
person a request belongs to, and a guarantee that the database refuses
cross-person reads even when application code forgets.

### Options weighed

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

### Decision

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
   `services/api/src/harrier/tracker/`, from which SQLite and Postgres DDL
   are produced; the hosted-only columns and policies are part of that
   definition, marked hosted. Migrations stay harrier's own runner (spec
   090), which gains a Postgres dialect. Postgres DDL is transactional, so
   spec 090's whole-or-nothing rule holds natively. Supabase CLI migrations
   are not used, because they would be a second schema definition.
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

### Consequences

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

### Honest limitations

- Nothing hosted exists after this ADR. It is a decision and a sequence.
- Row-level security protects rows only when the connection runs as
  `authenticated`. A code path that uses the service role bypasses it. The
  guard is decision 3's rule plus a test in spec 105, not the database.
- Google's restricted Gmail scopes need app verification and a security
  assessment before external users can grant them. Hosted Gmail watch may
  not be available at first; spec 109 decides.
- Running LinkedIn and academic searches through Apify on tenants' behalf
  depends on each tenant's own Apify account and the terms they accept.

## Behavior

After this change, the observable differences are in the documents and the
compiled governance only.

The Local-first bullet in `.ai/rules/product-invariants.md` reads:

> **Local-first.** The local product has no cloud dependencies beyond
> Apify, the AI providers, Telegram, Gmail API, and Hunter. The hosted
> deployment (ADR-013) adds Supabase, Fly.io and Vercel, and offers only
> the API LLM providers. All LLM calls go through the provider seam in
> `services/api/src/harrier/llm/`, selected by env (`AI_PROVIDER`),
> pluggable across codex-cli, claude-cli, openai-api, anthropic-api.

The first bullet of `.ai/rules/privacy.md` reads:

> All personal data (candidate profile, resume truth sources, bullet pool,
> application narrative, interview prep, tracker rows, contacts) lives in
> the local database or local files, or, in the hosted deployment, in the
> owner's rows and storage objects under row-level policy (ADR-013). It is
> never in git in any form. Never paste its contents into public files,
> fixtures, tests, code, or commit messages.

Every other invariant is unchanged, including "Nothing auto-sends" and
"Telegram notifications are the only outbound messages".

## Follow-on specs

Each line is a stub created by this spec with `approved: no`. Order is the
build order; each depends on the ones above it unless noted. Numbers 098 to
101 are not used here: spec 097 lists them as later academic-track work.

- **103 The tracker store speaks Postgres.** One schema definition produces
  SQLite and Postgres DDL; the migration runner applies either; tracker
  tests pass against both. No auth, no hosting.
- **104 The hosted API authenticates every request.** JWT verification,
  401 on a missing or invalid token, request transactions as
  `authenticated`, CORS for the Vercel origin, and which spec 035 guards
  carry over.
- **105 Every personal table is owned and policed.** `owner_id`, RLS
  enabled and forced, and a test that lists every table with personal data
  and fails if one lacks a policy; a second test proving owner A reads zero
  rows of owner B.
- **106 Tenant credentials and run isolation.** Encrypted per-owner
  credentials, runs record and run as their owner, a run's environment
  holds only its owner's credentials, hosted provider list.
- **107 Artifacts and run state in Storage.** File writes listed in ADR-013
  decision 6 go through one storage seam: the local file system locally,
  the owner-prefixed private bucket hosted.
- **108 Hosted scheduling.** The scheduler that replaces launchd hosted:
  where it runs, how it lists due owners, how a failed owner run is
  reported without blocking the others.
- **109 Gmail and Telegram for tenants.** Per-owner OAuth and bot
  configuration, and whether Gmail ships in the first hosted release.
- **110 Deploy pipeline and environments.** Fly app, Vercel project and
  Supabase project; migrations applied before the new API version serves;
  secrets kept out of git; the environments named under Open decisions.
- **111 Sign-up, export and deletion.** Who can create an account, and an
  owner's ability to export and delete everything they own.

## Failure modes

This spec ships documents, so its failures are documents that disagree.

- **Generated governance hand-edited.** If `CLAUDE.md` or `AGENTS.md`
  change without the matching `.ai/` source change, the source-of-truth
  rule is broken. The diff must show the `.ai/` edit and an `aie sync`
  output that matches it.
- **An ADR stays silent.** If ADR-012 or ADR-009 still say the hosted
  question is open after ADR-013 lands, a reader gets two answers. Both
  status lines change in the same diff.
- **A stub reads as approved scope.** A stub with `approved: yes`, or one
  whose text an implementer could build from, invites building before
  refinement. Stubs carry `approved: no` and a Problem paragraph only.
- **Invariant text drifts from the ADR.** The amended bullets must name
  ADR-013 and the same three vendors the ADR names.
- **Code changes ride along.** Any change under `services/`, `apps/`,
  `packages/`, `config/`, `.github/` or `.gitignore` is out of this spec.

## Acceptance criteria

- [x] `docs/adr/ADR-013-hosted-multi-tenant-deployment.md` exists and its
      Context, Options weighed, Decision, Consequences and Honest
      limitations match this spec's ADR-013 section.
- [x] `.ai/rules/product-invariants.md` and `.ai/rules/privacy.md` contain
      the two bullets under Behavior verbatim.
- [x] `aie sync` was run and `CLAUDE.md`, `AGENTS.md` and generated rule
      files differ from `main` only by those two bullets.
- [x] ADR-012's and ADR-009's status lines name ADR-013; no other line of
      either changes.
- [x] `docs/architecture.md` has a "Hosted deployment (decided, not built)"
      section naming ADR-013 and specs 103 to 111.
- [x] `specs/103-*.md` to `specs/111-*.md` exist, each with `approved: no`
      and `status: proposed`.
- [x] `git diff --stat main` lists only paths under `docs/`, `specs/`,
      `.ai/`, `.claude/` generated files, `CLAUDE.md` and `AGENTS.md`.
- [x] `just check` passes.

## Honest limitations

- This spec makes nothing deployable. Its only value is that the nine
  specs after it build against one decision.
- The vendor capabilities ADR-013 relies on (`auth.uid()` in policies,
  JWT verification against published keys, Storage policies, Fly machines
  running a long-lived container) are taken from the vendors' documented
  behavior, not from a harrier prototype. Specs 103 and 104 are where a
  wrong assumption would surface first.
- No cost figures are given. Pricing depends on tenant count and run
  volume, neither of which exists.

## Migration

None. No local install changes, and no hosted install exists.

## Options weighed

The ADR's options cover the store. Two choices about this spec itself:

- **Carry ADR-013 with the first implementation spec, as spec 091 carried
  ADR-012.** Rejected: the hosted build is too large for one change, and
  `change-boundary` says to split the spec, not the branch. The decision
  goes first, alone.
- **Write all nine follow-on specs in full now.** Rejected: each depends on
  what the one before it finds. Stubs sequence; refinement happens one at a
  time.

## Open decisions for Akin

Akin took the recommended answer to each on 2026-10-10. They bind the
follow-on specs named against each.

1. **Local backend: SQLite locally, Postgres hosted.** Two dialects,
   ADR-013 decisions 5 and 9. The rejected alternative was Postgres
   everywhere, which would make the fresh-clone demo need a database
   server. Binds spec 103.
2. **Region: EU.** Fly and Supabase both run in the EU, given the target
   users and GDPR. The exact regions are spec 110's.
3. **LLM calls: each tenant brings an API key.** Operator-paid calls would
   be their own spec, if ever. Binds spec 106.
4. **Environments: staging and production,** each a separate Supabase
   project, Fly app and Vercel environment. Binds spec 110.
5. **Sign-up: invite-only** until spec 111 ships export and deletion.
   Binds spec 111.

## Proof / origin

- `docs/adr/ADR-012-search-tracks-and-tenant-isolation.md`, Decision point
  2 and Consequences: the list of what ADR-013 owns.
- `docs/adr/ADR-009-user-configuration-and-tenancy.md`, Decision point 3.
- `specs/091-every-tracker-row-belongs-to-a-search-track.md:490`.
- `specs/090-migrations-apply-whole-or-not-at-all.md:243`.
- `docs/adr/ADR-004-long-running-work.md`: in-process run manager with
  subprocesses, the reason the API needs a long-lived host.
- `services/api/pyproject.toml` `pdf` group and `Dockerfile`: Chromium and
  poppler, the reason the API image is a container.
- `.env.example`: the per-tenant credentials decision 8 lists.

## Out of scope

- Any code, schema, migration, contract, CI workflow or classification
  change. Those belong to specs 103 to 111.
- Creating Supabase, Fly or Vercel projects or accounts.
- Pricing, billing and plans.
- A tenant CLI.
- Changes to the local product's ADRs beyond the two status lines.
- Moving the local product off SQLite (Open decision 1 records it; a
  separate spec would do it).
