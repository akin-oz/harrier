# Harrier target architecture

Ground-up rewrite of `~/job-hunt-local`: local-first job search automation as a public
monorepo. This doc is the map; each firm decision cites its ADR. Requirements baseline:
the old repo's `CLAUDE.md` and `OPERATIONS.md`, inventoried in
`docs/parity-matrix.md`.

## System shape

Three execution surfaces over one domain core, one store, one contract:

```
launchd (schedule)          browser (GUI)              terminal
      |                          |                         |
      v                          v                         v
 harrier CLI  ----shares---> FastAPI service <---- generated TS client
 (harrier_cli)   domain code  (harrier_api)         (packages/contract)
      |                          |                        |
      +---------> harrier domain package <----------------+
                  (tracker, screening, scoring,        (types only)
                   resume, apply, offers, outreach, mail)
                         |
                  SQLite (data/tracker.db)  +  file stores (state/, runtime/)
```

- The **domain package** (`services/api/src/harrier/`) owns all behavior: the tracker
  write path, the screening pipeline, scoring, artifact generation, outreach, mail
  classification. It imports nothing from the API layer (lint-enforced, ADR-007).
- The **CLI** (`harrier_cli`) and the **API** (`harrier_api`) are thin shells over the
  same domain calls. Scheduled runs use the CLI (ADR-006); interactive runs go through
  the API's run manager, which executes the same CLI entry points as subprocesses
  (ADR-004). Behavior cannot fork between surfaces because there is one code path.
- The **web app** (`apps/web`, Vite + React, strict TS, Feature-Sliced Design, ADR-001)
  talks only to the API through the generated client (ADR-005).

## Module boundaries inside the domain

Mirrors the old system's four planes plus generation and outreach, with the boundaries
the old code implies but does not enforce:

| Module | Owns | Old-repo origin |
|---|---|---|
| `harrier.tracker` | The single write path, status lifecycle, selectors, dedupe indexes, CSV export | `scripts/jobs.py`, tracker parts of `job_sources.py` |
| `harrier.screening` | Normalized job shape, gate order, remote/EMEA policy, scoring, archetypes | `scripts/job_sources.py` |
| `harrier.sources` | One ingestion module per source, each returning normalized jobs only | `scripts/import_*.py` |
| `harrier.resume`, `harrier.apply` | Resume, cover letter, answers; truth validation; PDF gates; label scrubbing | `tailor_resume.py`, `openai_cover_letters.py`, `application_answers_lib.py` |
| `harrier.offers` | Offer evaluation blocks, verdict contract, batch prospect evaluation | `evaluate_offer.py`, `evaluate_prospects.py` |
| `harrier.outreach` | Contacts store, staged discovery, fit scoring, drafts, queue state machine | `outreach_lib.py`, `find_contacts.py`, message generators |
| `harrier.mail` | Gmail poll, classification cascade, event log | `gmail_watch_lib.py` |
| `harrier.notify` | Telegram sending, digest assembly | `send_telegram.py`, `send_daily_digest.py` |
| `harrier.llm` | The provider seam: codex-cli, claude-cli, openai-api, anthropic-api, auto | `llm_client.py` |
| `harrier.profile` | Candidate config, application profile, resume facts, truth sources | `application_profile.py`, `resume_facts.py` |
| `harrier.tracks` | Search tracks: the `Track` and `Scope` values, the slug rule, the resolver (spec 091, ADR-012) | none; new |

Boundary rules carried from the product invariants:

- `harrier.sources` modules are ingestion only. They may not import scoring, filtering,
  or tracker code; they return normalized jobs and nothing else. The shared path in
  `harrier.screening` does the rest, once. (Old rule: OPERATIONS.md "Design rule".)
- All tracker mutation goes through `harrier.tracker`. The API, CLI, capture endpoints,
  and the one automated mutator (batch evaluation auto-reject) call it; nothing touches
  SQLite directly. (Old rule: "tracker is the only source of truth".)
- All LLM calls go through `harrier.llm`. The old repo has two bypasses
  (`evaluate_offer.py`, `tailor_resume.py` own OpenAI paths); the rewrite closes them.
- Draft, never send: `harrier.outreach` and `harrier.mail` have no send capability for
  outreach or email. Telegram notifications are the only outbound messages.

## Contract seams

Three seams, each with a generated artifact, a drift gate, and a pre-edit guard:

1. **API contract** (ADR-005): FastAPI exports `packages/contract/openapi.json`
   deterministically; `openapi-typescript` generates the TS types; CI regenerates and
   fails on diff; the web app can only speak in generated types.
2. **Tracker schema** (ADR-003): one schema definition with migrations in
   `harrier.tracker`; the CSV export shape is part of the contract and tested. The old
   repo's triplicated column list collapses to this single point.
3. **Governance sources** (`.ai/`): `aie sync` generates `CLAUDE.md`, `AGENTS.md`,
   `.claude/**`; `aie check` in CI proves no drift; generated files are never
   hand-edited (gaps go to `docs/aie-feedback.md`).

## Data flow

**Discovery (scheduled or interactive)**

```
feeds.txt / linkedin_search_urls.txt
  -> harrier.sources.<source>.fetch()        (ingestion only)
  -> normalize to the shared job shape
  -> harrier.screening.screen():             seen-state -> hold list -> title
                                             -> remote/EMEA -> tracker dedupe
                                             -> enrich -> score (ranks; spec 033
                                                removed the cutoff)
  -> harrier.tracker.add_prospects()         (one write path, one transaction)
  -> per-source summary + run summary        (incoming/ shape preserved)
  -> one aggregated Telegram notify
```

Interactive runs wrap this in the run manager: `POST /runs` spawns the CLI as a
subprocess; stdout's structured progress lines become SSE events
(`log_line`, `progress`, `state_change`, `result`) consumed live by the GUI (ADR-004).

**Application workflow**

```
GUI/CLI: shortlist -> tailor -> applied -> interviewing | rejected
tailor: harrier.resume.tailor_resume()
  -> content plan (LLM selects bullet IDs; require_truth validates against truth sources)
  -> markdown -> HTML template -> Playwright PDF -> validate_rendered_pdf
  -> success only if the PDF exists; tracker row updated only on success
applied: seeds follow-up date + outreach block (needs_contacts, find contacts, high)
```

**Outreach**

```
applied row -> staged contact discovery (Apify profile search)
  -> runtime/outreach/<slug>-candidates.json   (staged, never direct)
  -> human approves in GUI -> harrier.outreach.add_contact()
  -> drafts (AI or template path) -> human sends manually -> mark sent/replied
```

**Mail and digest**

```
launchd (5 min) -> harrier.mail.poll(): Gmail readonly -> classification cascade
  -> event log -> Telegram for actionable kinds -> tracker row matching
20:30 -> harrier.notify.digest(): new prospects, top prospects, outreach due,
  ghosted (>21 days), mail events
```

**Capture**

```
bookmarklet -> GET/POST /capture/add on the API (mixed-content-safe HTML response kept)
  -> harrier.tracker.add_manual() -> 200/400/409/500 contract preserved
```

## Data at rest

Per ADR-002 as revised by ADR-008: source and fixtures public; everything
personal (candidate and application content, tracker, contacts, artifacts,
state, logs) lives in the local database and local files, never in git in any
form; credentials never in git. Demo mode boots from `fixtures/` with zero
secrets. gitleaks guards every commit and CI run; a coverage test binds the
classification table to `.gitignore`.

## Governance chain

The Sorrel chain (a private sibling project of the author's, README
"Spec-gated quality enforcement"), expressed through `.ai/` sources and compiled by
`aie`, with hooks the compiler cannot express wired directly into
`.claude/settings.json` (the compiler preserves entries it does not own):

1. Specs in `specs/`, `approved: yes` gates implementation, only Akin flips approval.
2. Turn-end gate: pyright + tsc + unit tests; red fails the turn (exit 2 Stop hook).
3. Pre-edit guard pauses on: `packages/contract/**`, `harrier/tracker/` schema and
   migrations, the classification config and `.gitignore`, `.github/workflows/**`, `.ai/**`
   generated outputs.
4. Every commit carries `Spec: NNN`; CI resolves trailers to approved specs.

Details and the review-agent set land with deliverable 4 (checkpoint B).

## Hosted deployment (decided, not built)

ADR-013 decides how harrier is offered to other people: Supabase Postgres
with row-level security keyed by the authenticated owner, Supabase Auth and
Storage, the API and run workers on Fly.io, and the SPA on Vercel. Domain
SQL still names a track and never a tenant; the database applies the owner
from the session (ADR-012 point 2). The local product described above does
not change.

A Postgres store can be created, and the domain runs on it. The hosted
API serves it (below); outside `harrier store`, only a hosted run the API
starts for one owner reads or writes tracker rows there (spec 106, below).
`HARRIER_DATABASE_URL` chooses the store: unset or empty, the local
SQLite store; a `postgresql://` URL, the Postgres store; any other value is
refused (`services/api/src/harrier/connection.py`,
`services/api/tests/test_postgres_store.py`). `harrier store migrate`
applies pending migrations and `harrier store status` prints the dialect
and version. They are the only commands a person runs on Postgres: a run
command runs there only as a hosted run, every other command refuses to
start while the URL names it until spec 127, and the
API serves it only in hosted mode (`services/api/tests/test_store_cli.py`,
`services/api/tests/test_hosted_start.py::test_the_api_serves_postgres_only_in_hosted_mode`).
Opening a Postgres store never migrates it. The Postgres baseline, recorded as version 9, sits in
`services/api/src/harrier/tracker/schema.py` beside the SQLite migrations,
and `services/api/tests/test_dialect_parity.py` holds the two stores to the
same tables, defaults and refusals.

The domain runs on either store (specs 112 and 126). Every domain function
takes harrier's own `Connection`
(`services/api/src/harrier/connection.py`), which speaks both dialects, and
each statement is written once. Timestamps come from `store_now()`, bound
as a parameter. The dedupe folds `A` to `Z` and nothing else
(`ascii_folded`), and text orders by code point (`by_code_point`). New ids
come from `RETURNING id`, and no domain insert supplies one. The four
upserts are an update, an insert in a savepoint, and the update once more
when another writer inserted the key first (`update_or_insert`); none names
a tenant. Domain code branches on the dialect in three places. The write
lock (`_write_lock` in `services/api/src/harrier/tracker/store.py`): SQLite
still locks the whole file, and Postgres locks the job rows a write depends
on, `FOR UPDATE`, so writers of other jobs do not wait. The configuration
kinds a store holds (`kinds_for` in
`services/api/src/harrier/userconfig/store.py`, spec 108). And the digest,
which on Postgres reads no mail file and names no mail watch, because
Gmail is not offered there (`mail_offered` in
`services/api/src/harrier/digest.py`, spec 109). The cutover and the
legacy import refuse a Postgres connection with `StoreLocalOnlyError`. The
`store` fixture in `services/api/tests/conftest.py` runs a test once per
store, on Postgres as one synthetic owner under the row policy, and fails
it unless the connection is `harrier_tenant`
(`services/api/tests/test_domain_sql_on_both_stores.py`, and the lock tests
in `services/api/tests/test_job_events.py`, such as
`services/api/tests/test_job_events.py::test_a_decision_on_one_job_does_not_wait_for_another`).

On Postgres every personal row has an owner (spec 105). Migration 10 in
`schema.py` gives each table in `OWNED_TABLES` an `owner_id` column that
references `auth.users`, and one policy,
`owner_only`: the policed role, `harrier_tenant`, sees and stores only rows whose
`owner_id` is its own. Since migration 16 the column defaults to
`harrier_owner_id()`, and the policy compares with it: a function in
`public`, executable by the tenant role alone, that reads the `sub` of the
request's `request.jwt.claims`. The tenant role needs nothing in schema
`auth` and holds nothing there
(`services/api/tests/test_owner_policy.py::test_the_tenant_role_holds_nothing_in_auth`,
`services/api/tests/test_owner_policy.py::test_harrier_owner_id_reads_the_request_claims`). Row security is enabled and forced on every table.
Every unique key on an owned table leads with `owner_id`, except the
primary key `id` of the tables numbered from one global sequence
(`GLOBAL_ID_TABLES`): the tenant role may write every column of those
tables but `id` (migration 13), so no key a tenant can write into tells one
owner what another holds
(`services/api/tests/test_owner_policy.py::test_an_owner_cannot_choose_or_change_an_id`).
Each owner has their own track 1, and a job or event can reference only its
own owner's track or job. Migrations 10, 13, 14, 16, 17, 18 and 19 are Postgres only
(`SINGLE_DIALECT_MIGRATIONS`): SQLite records those versions and changes
nothing, so the local schema names no tenant. The catalog test,
`services/api/tests/test_owner_policy.py::test_every_table_is_owned_or_declared`,
fails naming any table that is neither owned nor declared unowned, lacks
the column, the forced policy or an owner index, has a unique key that
breaks the rule above, a rule, or an owner other than the migrating role,
or carries an unexpected table, column, sequence or function grant, view,
definer function, default privilege, tenant role attribute or
membership, or anything in `public` that still reaches `auth.uid()`.
Supabase's own functions in `public` are declared in
`SUPABASE_FUNCTIONS_IN_PUBLIC` and skip the function rules only while they
return `event_trigger`
(`services/api/tests/test_owner_policy.py::test_a_declared_supabase_function_is_not_a_catalog_problem`).
The isolation tests in the same file act as two synthetic owners
(`services/api/tests/test_owner_policy.py::test_owner_a_reads_nothing_of_owner_b`,
`services/api/tests/test_owner_policy.py::test_owner_a_cannot_write_as_owner_b`).
Supabase's Data API role, `authenticated`, holds no privilege on any harrier
table, so harrier's write path is the only one
(`services/api/tests/test_owner_policy.py::test_the_data_api_role_reaches_no_harrier_table`). Plain Postgres has none of
Supabase's auth objects, so `services/api/tests/pg_support.py` installs a
test shim before migrating: the `anon`, `authenticated` and `service_role`
roles, an `auth.users` table, an `auth.uid()` function, and Supabase's
default privileges, which grant those roles everything the migrating role
creates, in `public` and in every schema. No migration creates them, and
migration 10 refuses a store without the roles and the auth objects.

The hosted API is the one path an owner reaches their rows by (below):
the CLI admits a Postgres URL only for `harrier store`, for the hosted
runs the API starts (spec 106) and for `harrier operator` (spec 108),
until spec 127. A
role with `BYPASSRLS`, such as Supabase's `service_role`, sees every owner's
rows
(`services/api/tests/test_owner_policy.py::test_a_bypassrls_role_sees_every_owner`);
the hosted API's start refuses a login role that can reach one (spec 134).

The shim is not Supabase, so the isolation tests also ran on a real
project: a Supabase staging project on Postgres 17.6, migrated by
`harrier store migrate` as Supabase's own `postgres` role, on 2026-10-11
(spec 115). `services/api/tests/test_supabase_project.py` holds one test
per assumption spec 105 rests on and runs the isolation tests of
`test_owner_policy.py` in staging mode, against the project instead of a
fresh shimmed database. It skips without the project's URLs, in CI too, so
it is a recorded check and not a gate. The run refuted one assumption: the
migrating role cannot give `harrier_tenant` usage on schema `auth`, so a
tenant statement that names `auth.uid()`, such as the trigger that numbers
a new track, is refused there. Spec 115's "Amendment (staging findings)"
records every result and the spec that owns each fix. Migration 16 is that
fix (spec 105, amendment of 2026-10-11). What the run found is kept in CI:
every Postgres test now migrates and connects as `harrier_migrator`, a role
that is not a superuser and holds in the shim what `postgres` holds on the
project, USAGE on `auth` without the grant option included
(`services/api/tests/test_owner_policy.py::test_ci_migrates_as_a_non_superuser`,
`services/api/tests/test_owner_policy.py::test_the_shim_migrator_holds_auth_usage_without_grant_option`).
A store stopped at version 15 refuses an owner's second track there, as the
project did
(`services/api/tests/test_owner_policy.py::test_track_two_needs_migration_16_on_the_faithful_shim`).

Hosted mode is explicit (spec 104). `HARRIER_AUTH=supabase` turns it on,
read from the process environment only, never from a `.env` file. It then
requires `HARRIER_SUPABASE_URL`, `HARRIER_ALLOWED_HOSTS`,
`HARRIER_CORS_ORIGINS` and a Postgres `HARRIER_DATABASE_URL`, and refuses
to start naming the variable when one is missing or malformed. Demo mode,
a missing `hosted` dependency group and a service credential in the
environment refuse the start too
(`services/api/src/harrier_api/hostedauth.py`,
`services/api/tests/test_hosted_auth.py::test_hosted_start_refuses_incomplete_configuration`).
Unset or empty, the API is the local API of spec 035, unchanged
(`services/api/tests/test_hosted_auth.py::test_local_mode_has_no_hosted_middleware`).
The hosted app needs a Supabase access token in the `Authorization`
header on every route but `GET /health`. An ASGI middleware checks it
before routing against the project's published ES256 or RS256 keys and
answers a refusal with `AuthRefusalOut`: 401, 403 for an anonymous
session, or 503 while the keys cannot be fetched. It trusts only the
configured host names and sends CORS headers only to the configured
origins. The contract declares the 401 and the bearer scheme on every
route that needs the token
(`services/api/tests/test_hosted_auth.py::test_the_contract_declares_every_auth_refusal`),
in both modes, so there is one document
(`services/api/tests/test_hosted_auth.py::test_the_contract_is_the_same_in_both_modes`).
Hosted, spec 035's local token is not read: the bearer token serves its
purpose
(`services/api/tests/test_hosted_auth.py::test_the_local_token_is_not_read_hosted`).
The hosted API serves the Postgres store, and only it does: without
hosted mode a Postgres URL is refused at start
(`services/api/tests/test_hosted_start.py::test_the_api_serves_postgres_only_in_hosted_mode`,
`services/api/tests/test_hosted_start.py::test_create_app_serves_a_complete_hosted_configuration_hosted`).
Each request runs as its user (spec 134,
`services/api/src/harrier_api/hostedstore.py`). It takes a connection from
a pool and opens one transaction through the seam: `harrier_tenant`, the
verified claims, and a read-back that the role, the claims' `sub` and
`harrier_owner_id()` are the token's, in every owner transaction, runs
included
(`services/api/tests/test_hosted_requests.py::test_the_request_transaction_runs_as_its_user`,
`services/api/tests/test_hosted_requests.py::test_the_read_back_checks_the_owner_the_policy_reads`).
Its lock, statement and idle timeouts are `SET LOCAL` in every request
(`services/api/tests/test_hosted_requests.py::test_the_request_role_has_lock_and_statement_timeouts`).
A middleware commits below 400 and rolls back otherwise before the client
sees the status
(`services/api/tests/test_hosted_requests.py::test_a_refused_request_commits_nothing`).
The API connects as a dedicated login role, `harrier_api`, `NOINHERIT` and
a member of `harrier_tenant` and of nothing else. The operator creates it
with a password (`CREATE ROLE harrier_api LOGIN NOINHERIT`) and grants it
`harrier_tenant`, which spec 110's `harrier store grant-tenant` is to do with
the same checks. At start the API refuses a login role that is a superuser,
has `BYPASSRLS`, owns a table in `public`, cannot set `harrier_tenant`, or
can reach a role past the policy through any membership
(`services/api/src/harrier/loginrole.py`,
`services/api/tests/test_hosted_start.py::test_the_login_role_checks_name_each_failure`),
a store whose default isolation is not `read committed`, and a store not at
the version the code needs
(`services/api/tests/test_hosted_start.py::test_hosted_start_checks_the_store_version_once`).
Hosted logs go to stderr only, which the platform collects. The hosted
start opens no local SQLite store and writes no log file
(spec 135, `services/api/tests/test_hosted_logs.py::test_a_hosted_start_opens_no_local_store`),
and a driver error's text, which quotes the row, never reaches a log record
(`services/api/tests/test_hosted_logs.py::test_a_failed_statement_logs_none_of_its_row`).
The access line drops the query
(`services/api/tests/test_hosted_logs.py::test_a_hosted_access_line_carries_no_query`),
and the start refuses prompt logging
(`services/api/tests/test_hosted_logs.py::test_a_hosted_start_refuses_prompt_logging`).

Each owner's credentials (an AI key, an Apify token, a Hunter key, a
Telegram bot) are stored encrypted (spec 116,
`services/api/src/harrier/credentials.py`). The `credentials` table is
Postgres only, created by migration 18 and declared in
`HOSTED_ONLY_TABLES`: locally credentials stay in `.env`, and the parity
test leaves the table out and fails when the declaration names a table
Postgres lacks or SQLite has
(`services/api/tests/test_dialect_parity.py::test_a_hosted_only_table_is_declared_for_parity`).
It is owned and policed as every owned table is. Each row is AES-256-GCM
over the kind's fields, with a fresh nonce per write and the owner and kind
as associated data, so a ciphertext moved to another owner's row or kind
does not decrypt
(`services/api/tests/test_credentials.py::test_a_ciphertext_moved_to_another_owner_or_kind_does_not_decrypt`,
`services/api/tests/test_credentials.py::test_the_database_never_holds_a_credential_in_plain_text`).
The key is `HARRIER_CREDENTIALS_KEY`, a Fly secret on the API app; the
hosted API refuses to start without a well formed one
(`services/api/tests/test_credentials.py::test_hosted_start_needs_a_well_formed_credentials_key`).
`GET /credentials` says which kinds are set and when, and `PUT` and
`DELETE` set and remove one; no route reads a value back, and a refused
body is answered with the field and the rule, never the value
(`services/api/src/harrier_api/credentials_routes.py`,
`services/api/tests/test_credentials.py::test_credential_routes_never_return_a_value`,
`services/api/tests/test_credentials.py::test_a_refused_credential_body_does_not_echo_the_value`).
Locally every credentials route answers 404
(`services/api/tests/test_credentials.py::test_credential_routes_are_not_served_locally`).
Only a hosted run's start decrypts one (spec 106, below). A Postgres
store that recorded a version above a declared migration it never applied
is refused by `harrier store migrate`, the store's opener and the hosted
start, since the runner applies only higher versions
(`services/api/tests/test_credentials.py::test_a_store_that_skipped_a_declared_migration_is_refused`).

A hosted run is bound to its owner (spec 106). Spec 106's table,
`HOSTED_RUN_KINDS` in `services/api/src/harrier_api/runs.py`, gives each
run kind its hosted answer: discovery, the demo run, the digest, the feed
check, reconsideration, the events backfill and the prospect evaluation
run; a kind whose result is a file answers 501 until spec 107, Gmail
watch is spec 109's, and backup and its verification are local only
(`services/api/tests/test_hosted_runs.py::test_every_run_kind_declares_its_hosted_answer`,
`services/api/tests/test_run_owner.py::test_a_kind_not_offered_hosted_answers_501_naming_its_spec`).
Two owners never join each other's run
(`services/api/tests/test_hosted_runs.py::test_two_owners_never_join_each_others_run`).
At start the run manager reads the owner's ciphertexts as the owner,
decrypts them, refuses with 409 a run that needs a credential that is unset
or does not decrypt, and inserts the run's row in `run_records`, all in one
transaction before anything is spawned
(`services/api/src/harrier_api/hostedruns.py`,
`services/api/tests/test_hosted_runs_on_postgres.py::test_a_route_refuses_a_run_whose_credential_is_missing_or_unreadable`).
The child's environment is built, never inherited, by
`services/api/src/harrier_api/runenv.py`: a short allowlist of
configuration names, the owner's run directory as `HOME`,
`HARRIER_DATA_DIR` and working directory, the run login's URL as its
`HARRIER_DATABASE_URL`, `HARRIER_RUN_OWNER`, and the owner's own
credentials under the names the domain reads
(`services/api/tests/test_hosted_runs.py::test_the_child_environment_holds_only_the_allowlist_and_its_owners_values`,
`services/api/tests/test_hosted_runs.py::test_the_credentials_key_never_reaches_a_run`,
`services/api/tests/test_hosted_runs.py::test_a_hosted_run_works_in_its_owners_directory`).
With `HARRIER_RUN_OWNER` set, `harrier_cli` reads no `.env`, runs only the
run class's verbs, on a Postgres store, and opens it with
`open_store(owner=...)`, every transaction as the owner
(`services/api/tests/test_hosted_runs.py::test_a_hosted_run_reads_no_env_file`,
`services/api/tests/test_hosted_runs.py::test_a_run_command_needs_an_owner_on_postgres`,
`services/api/tests/test_hosted_runs_on_postgres.py::test_a_run_reads_only_its_owners_rows`).
A hosted run offers only the API LLM providers
(`services/api/tests/test_hosted_runs.py::test_hosted_runs_offer_only_the_api_providers`),
reads its log redaction values from its own store
(`services/api/tests/test_hosted_runs_on_postgres.py::test_a_hosted_run_redacts_logs_from_its_own_store`),
and every exact secret value of its owner's credentials is replaced with
`REDACTED` in its events
(`services/api/tests/test_hosted_runs.py::test_a_credential_value_never_reaches_the_event_stream`).
Runs connect as a second login role, named by `HARRIER_RUN_DATABASE_URL`,
which the API's start holds to the same login role checks and refuses when
it is the API's own
(`services/api/tests/test_hosted_runs_on_postgres.py::test_hosted_start_checks_the_run_login_role`).
While any run is active the run manager sends `GET /health` through Fly's
proxy so the machine is not stopped under it
(`services/api/tests/test_hosted_runs.py::test_an_active_run_keeps_the_machine_awake`),
and a run cut off by a stop anyway is shown `interrupted` from
`run_records`, migration 19
(`services/api/tests/test_hosted_runs_on_postgres.py::test_a_run_cut_off_by_a_stop_reads_interrupted`,
`services/api/tests/test_hosted_runs_on_postgres.py::test_the_stop_signal_records_active_runs_interrupted`).

The trust model bounds all of this (spec 106, The trust model). Row-level
policy catches a bug in a run: a query that forgets its filter still sees
only the owner's rows. It does not contain a compromised run. The run
login can set any owner's claims, and every run and the API are one
operating system user on one machine, so a hostile run can claim any owner
in the database and can likely read its siblings' environments. The
environment allowlist keeps secrets out of logs and child tools; it is not
a sandbox. Hosted harrier treats its own code and dependencies as trusted.

The hosted schedule is `harrier operator scheduler` (spec 108,
`services/api/src/harrier_api/hostedscheduler.py`; its rules and writes are
`services/api/src/harrier/hostedschedule.py`). An owner's choice of jobs and
timezone is the `schedule` kind of their configuration, and every owner
shares the committed cadence in `config/schedule.json`. A Fly scheduled
machine starts the command on an hourly cycle; each start takes a session
advisory lock or stands by, lists every owner's choice and state, claims each
due slot as its owner, starts the run through spec 106's binding with the
owner's `TZ`, waits, records how each ended in `schedule_state`, and exits
(`services/api/tests/test_hosted_scheduler.py::test_one_scheduler_start_runs_one_tick_and_exits`,
`services/api/tests/test_hosted_scheduler.py::test_a_second_scheduler_stands_by`).
Its login, `harrier_scheduler`, is a member of `harrier_tenant` and of
`harrier_schedule_reader` and nothing else, and the start refuses a login
that can step past the policy
(`services/api/tests/test_hosted_scheduler.py::test_the_scheduler_refuses_a_login_that_can_bypass`).
The one read across owners is `harrier_schedule_inputs()`, in a transaction
as the reader that sets no claims; every other transaction runs as
`harrier_tenant` with one owner's claims
(`services/api/tests/test_hosted_scheduler.py::test_the_scheduler_acts_as_each_owner`).
The API and the run login refuse to start as a member of the reader
(`services/api/tests/test_hosted_scheduler.py::test_the_api_login_cannot_list_schedules`).
A slot is an instant in the owner's zone
(`services/api/tests/test_hosted_slots.py::test_the_due_slot_is_local_to_the_owner`),
starts at most once even when two connections claim it
(`services/api/tests/test_hosted_scheduler.py::test_a_slot_starts_once`),
and after an outage only the latest slot of each job starts
(`services/api/tests/test_hosted_scheduler.py::test_an_outage_starts_each_jobs_latest_slot_once`).
One owner's failing, hanging or refused run is recorded for that owner and
the loop goes on
(`services/api/tests/test_hosted_scheduler.py::test_one_owner_failing_does_not_stop_another`).
`harrier operator schedule-report` prints one line per owner and job and
exits 1 on a problem
(`services/api/tests/test_hosted_scheduler.py::test_the_report_exits_one_on_each_problem`).
`harrier operator` is one command group that runs only on Postgres, each
subcommand declaring its database role
(`services/api/tests/test_operator_class.py::test_the_operator_class_runs_only_on_postgres`,
`services/api/tests/test_operator_class.py::test_every_operator_subcommand_declares_its_role`).
The scheduler and the API are one image; the machine and its limits are spec
110's.

Gmail watch is local only (spec 109). A hosted watch would store and
transmit restricted Gmail data on servers, which Google allows only after
a security assessment. Hosted, `GET /mail/events` and `POST /mail/watch`
answer 501 and read nothing
(`services/api/tests/test_tenant_notifications.py::test_hosted_mail_routes_answer_501`),
the run manager refuses a `gmail-watch` before it spawns, also once spec
106 lets hosted runs start
(`services/api/tests/test_tenant_notifications.py::test_a_hosted_gmail_watch_run_is_refused_before_it_spawns`),
and a digest on a Postgres store opens no mail file, says Gmail watch is
not offered and leaves `mail-watch` out of its health lines
(`services/api/tests/test_tenant_notifications.py::test_a_postgres_digest_reads_no_mail_file_and_says_why`).
Each tenant connecting their own Google client is spec 118. Telegram
settings are per owner: the hosted API refuses to start while its
environment holds `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` or any `GMAIL_`
variable, naming it and never its value
(`services/api/tests/test_tenant_notifications.py::test_hosted_start_refuses_operator_notification_settings`).
The routes that store each owner's bot token and chat id wait for spec
116's credential store, and a run receives them through spec 106's run
environment. A failed Telegram send logs the scrubbed reason, never the
token
(`services/api/tests/test_tenant_notifications.py::test_a_telegram_failure_log_never_holds_the_token`).

No other hosted component exists yet. Specs 103 to 112 sequence the build:
the Postgres store (103, approved), API authentication (104), row-level
policy (105, approved), tenant credentials and run isolation (106),
artifacts in Storage (107), hosted scheduling (108), Gmail and Telegram for
tenants (109), the deploy pipeline (110), sign-up, export and deletion
(111), and the domain on either store (112).

## Honest limitations

- Single user, single machine. There are no user accounts. A local API token and a
  trusted-host check stop a web page in another tab from driving the API, but not a
  process running as the operator (spec 035; `services/api/tests/test_api_exposure.py`).
- Search tracks partition one person's tracker; they do not separate people. A tenant is
  a data directory, and the domain's SQL names a track and never a tenant (ADR-012,
  spec 091; `services/api/tests/test_tracks.py`).
- The run registry is not a durable queue; scheduled work bypasses it by design.
- macOS is the production platform (launchd); other platforms get manual CLI or cron.
- SQLite truth trades direct greppability for transactions; `just export` restores it.
- The demo shows the pipeline on synthetic data; Apify, Gmail, and Telegram paths need
  real keys and are stubbed in demo mode.
