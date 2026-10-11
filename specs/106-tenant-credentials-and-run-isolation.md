---
spec: 106
title: Tenant credentials and run isolation
status: accepted
approved: yes
milestone: M10
depends: [035, 047, 102, 104, 105, 112, 116]
---

# Spec 106: Tenant credentials and run isolation

Refined 2026-10-10 after approval of the stub. This text is an amendment
to the approved stub and goes to Akin before implementation.

## Problem

Credentials live in one `.env`, and a run's subprocess inherits the server's
whole environment (spec 035). Hosted, that would hand one tenant's run every
tenant's keys. ADR-013 decision 8 stores credentials per owner and gives
each run only its owner's. Akin chose bring-your-own LLM keys (spec 102,
open decision 3).

Line numbers below are from `main` at `44ac0d7` (2026-10-10), except the
`harrier_cli/main.py` and `harrier_api/app.py` ones, refreshed to `main` at
`524dd8a` on 2026-10-11, and the `schema.py` ones, refreshed to `main` at
`5c3d083` after migration 16. Counts say how they were taken.

The post-merge review of PRs #194 and #207 (2026-10-10) added five
problems this spec owns:

- **A run is a CLI process, and ADR-013 says there is no tenant CLI.**
  Every run kind is a `harrier_cli.main` command
  (`services/api/src/harrier_api/runs.py:39`, `KIND_COMMANDS`, and `:98`,
  `PARAMETERIZED_KINDS`; `build_command` at `:397` starts every argv with
  `harrier_cli.main`), spawned by `asyncio.create_subprocess_exec`
  (`runs.py:713-717`). ADR-013 decision 4 says "the hosted deployment has
  no tenant CLI", and spec 112 refuses every command but `harrier store`
  on a Postgres URL. This spec amends ADR-013 decision 4: a run is a CLI
  process bound by its parent, the API, to one owner, and asks spec 112
  for a refusal class "run command, owner required" (Changes to approved
  specs). (architecture 1)
- **The trust model is unwritten.** A run process connects with a login
  role that can set any claims it likes. Row-level policy catches a bug
  inside a run; it does not contain a compromised run, which can claim any
  owner. This spec states that limit. (architecture 1)
- **The child's environment is inherited.** `create_subprocess_exec` is
  called with no `env` (`runs.py:713-717`), and the CLI LLM providers copy
  `os.environ` again (`services/api/src/harrier/llm/providers.py:103`,
  `:157`). Hosted, the child's environment is built from an allowlist,
  never inherited, and excludes `HARRIER_DATABASE_URL` and every other
  server secret. (privacy 4, architecture 1)
- **The credential key has no home.** Spec 116 (split from this spec on
  2026-10-10) stores credentials encrypted and names the key's location.
  This spec decrypts an owner's credentials only at run start.
  (architecture 10)
- **Runs are per machine on Fly.** The run registry is an in-memory dict
  per process (`runs.py:580`). A stream request routed to another Fly
  machine answers 404, and Fly's auto-stop kills a machine's runs. Either
  one machine with auto-stop off, or `fly-replay` routing of run requests
  to the machine that holds the run, decided with spec 110.
  (architecture 8) The hosting budget of 2026-10-11 rules out auto-stop
  off: one machine stays, auto-stop is on, and this spec keeps the
  machine awake while a run is active and records a run cut off by a
  stop (Hosting budget (2026-10-11)).

Spec 105 routed one more here: a track id is no longer unique across
owners on Postgres, so every run key built from one, such as academic
discovery's `discovery:<track id>`
(`services/api/src/harrier/academic/discovery.py:69-71`), must be
qualified by owner hosted.

Refining the stub found six more, each of which would mix owners once
spec 104's 501 is lifted:

- **Two owners' runs join each other.** `RunManager.start` joins an active
  run with the same kind, target and signature (`runs.py:632-641`), and
  `active_run` matches on kind and target only (`runs.py:597-601`). The
  target is a track slug for reconsideration
  (`services/api/src/harrier_api/ops_routes.py:137`), a date for the
  digest (`ops_routes.py:217`), and empty for discovery started from
  `POST /runs` (`services/api/src/harrier_api/app.py:1111`). Slugs are per
  owner since spec 105, so owner B asking for a reconsideration of track
  `job` while owner A's runs would be handed A's run, with A's run id. And
  every discovery on the machine would share one lock.
- **Every run writes under one data directory.** `data_dir()`
  (`services/api/src/harrier/db.py:35-48`) is the root of every file a run
  writes: 40 lines in 26 files under `services/api/src` call it (`grep -rn
  'data_dir()'`, then `grep -rln`). Four of those paths are built from a
  track id (`academic/discovery.py:153`, `:172`,
  `screening/seen.py:85`, and the run key above, found with `grep -rnE
  'track\.id\}|str\((scope\.)?track(_id|\.id)\)'`). On one machine, owner
  A's track 1 and owner B's track 1 would share seen-state and saved
  datasets.
- **A run reads `.env`.** `main` calls `load_project_env()` first
  (`services/api/src/harrier_cli/main.py:3054-3055`), which copies every
  key of `.env` in the working directory that the environment lacks
  (`main.py:53-59`). An allowlist that leaves a name out would then be
  filled from a file. The image excludes `.env` (`.dockerignore:5-6`), so
  this is a guard against a file that should not be there, not a known
  leak.
- **`auto` can pick a CLI provider.** `resolve_auto_providers` tries the
  `codex` binary first and the `claude` binary last
  (`services/api/src/harrier/llm/config.py:123-138`), and the image
  installs the claude CLI (`Dockerfile:135`). ADR-013 decision 8 makes the
  CLI providers local only, and nothing enforces it.
- **A run's log redaction opens SQLite.** `_load_identity_values` opens
  `connect()` (`services/api/src/harrier/logsetup.py:123-140`). In a
  hosted run that is an empty SQLite file in the data directory, so logs
  would be unredacted.
- **Credentials are read from the environment by name.** `OPENAI_API_KEY`
  and `ANTHROPIC_API_KEY` (`llm/config.py:131-153`), `APIFY_TOKEN`
  (`sources/apify_linkedin.py:42`, `sources/apify_academic.py:51`,
  `outreach/discovery.py:56`), `HUNTER_API_KEY` (`outreach/hunter.py:23`),
  `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (`notify.py:32-33`), and the
  Gmail files (`mail/watch.py:597-599`). That is the seam this spec uses:
  a run's environment carries its owner's values under those names, and no
  domain module changes how it reads a credential.

## Scope

This spec was too large for one reviewable change, and was split on
2026-10-10 (Open decision 1). The credential store (owner credentials
encrypted at rest, the key's location, and the routes that set them) is
spec 116 and lands first. This spec is runs as their owner, including
every finding routed here. It reads what spec 116 stores through one
function.

1. **Run kinds declare their hosted answer.** `runs.py` gains
   `HOSTED_RUN_KINDS`: every kind either runs hosted, with the credentials
   it needs, or is refused with the spec that will offer it (Behavior).
   This replaces spec 104's single `RunsNotHosted` refusal (Changes to
   approved specs).
2. **The lock is per owner.** `active_run` and joining match on owner,
   kind and target.
3. **The child's environment is built, never inherited, hosted.** A new
   module, `services/api/src/harrier_api/runenv.py`, builds it from an
   allowlist, the run's own settings and the owner's decrypted credentials.
   `_run_process` passes it as `env` and the owner's run directory as
   `cwd`. Locally the child inherits, as spec 035 states.
4. **A run's database session runs as its owner.** Spec 112's opener
   takes an owner (Changes to approved specs): every transaction on a
   run's connection sets the role and the owner's claims first and reads
   them back (Behavior).
5. **A CLI refusal class "run command, owner required"** beside spec
   112's two, written into spec 112's Amendment (2026-10-11) with the
   operator class (decision D2). `harrier_cli.main` runs a run
   command on a Postgres URL
   only when `HARRIER_RUN_OWNER` names an owner, never reads `.env` in a
   hosted run, and refuses `HARRIER_RUN_OWNER` beside a SQLite store.
6. **Hosted runs offer only the API LLM providers.** `harrier.llm` refuses
   `codex-cli` and `claude-cli` in a hosted run, and `auto` resolves among
   the API providers only.
7. **A hosted run's log redaction reads its own store.**
8. **The owner's secret values are scrubbed from the run's events** at
   `RunManager._append`, the existing choke point (`runs.py:755-769`).
9. **A second login role for runs.** Hosted start-up requires
   `HARRIER_RUN_DATABASE_URL`, checked with spec 104's login-role checks
   (Changes to approved specs).
10. **Documents.** ADR-013 decision 4 gains an "Amended (spec 106)" note
    with the text under Behavior. `docs/architecture.md`'s hosted section
    states the trust model and names this spec's modules. `.env.example`
    gains `HARRIER_RUN_DATABASE_URL`, commented out, with a placeholder
    value.
11. **Changes this spec asks of approved specs** are listed under
    "Changes to approved specs". Each is written into its spec once Akin
    approves it with this one, and nothing else in those specs changes.
12. **A run's Storage identity is this spec's** (decision D3,
    2026-10-11). Binding a run to its owner includes the identity the run
    uses for Storage: `start` binds the run's `files()` (spec 107) to its
    owner before any domain code runs. Spec 107 consumes that binding and
    does not define it. How the run gets a token Storage accepts is Open
    decision 8.
13. **The machine stays awake while a run is active** (hosting budget,
    2026-10-11). Hosted, the run manager holds the API's machine up
    through Fly's proxy while any run is active (Hosting budget, Open
    decision 9).
14. **A run cut off by a stop is recorded, never lost** (hosting budget,
    2026-10-11). Hosted, each run has a row in a new hosted-only owned
    table, `run_records`, and a run whose machine stopped under it is
    shown and stored as `interrupted` (Hosting budget, Open decision 10).
    This adds one Postgres-only migration.

Classification: every new file sits under `services/api/src/`,
`services/api/tests/` or `specs/`, which `config/data-classification.json`
already classifies as public. Owner run directories sit under the machine's
`data/`, which is never-in-git. No new directory enters the repository.
This is checked before implementation, and a path the table does not
cover stops the change.

## Behavior

### The trust model

This is stated first because every other section is bounded by it.

| Holder | Holds | Can reach |
|---|---|---|
| The API process | `HARRIER_CREDENTIALS_KEY`, the API login URL, the run login URL | every owner's rows and every owner's decrypted credentials |
| A hosted run process | its owner's decrypted credentials, the run login URL, its owner's id | by design, its owner's rows only; if compromised, see below |
| The database | ciphertext only | no credential in plain text |

- **Row-level policy catches a bug.** A run's session runs as
  `harrier_tenant` with its owner's claims in every transaction (below).
  A domain query that forgets a filter, or a path that reads a row by id,
  sees only the owner's rows, because spec 105's policy compares
  `owner_id` with `auth.uid()`.
- **Row-level policy does not contain a compromised run.** The run login
  role can `SET ROLE harrier_tenant` and set any claims. Code running in a
  run process (a malicious dependency, an exploited parser of Apify, LLM
  or page output, the Chromium that renders a PDF) can claim any owner and
  read or write that owner's rows. The database cannot tell a forged claim
  from a real one, because no token is verified at the database.
- **The environment allowlist catches a leak, not an attacker.** Every run
  and the API run as one operating system user on one machine. On Linux,
  reading another process's `/proc/<pid>/environ` is governed by a ptrace
  read check (proc_pid_environ(5)), which a process of the same user
  normally passes. A compromised run can then read sibling runs'
  environments, with their owners' credentials, and the API's, with the
  key. The allowlist keeps secrets out of logs, crash output, child tools
  and error messages that print their environment. It is not a sandbox.
- **What would contain a compromised run** is a separate operating system
  user per run, or a machine per run, with a database credential that can
  claim one owner only. Options weighed records why neither is done here.

So the guarantee is: a hosted run that behaves as written touches only its
owner's rows, files and credentials, and a bug in domain code cannot widen
that. A hostile run is not contained. Hosted harrier treats its own code
and dependencies as trusted, the same trust the local product places in
them.

### Which kinds run hosted

`HOSTED_RUN_KINDS` maps every key of `KIND_COMMANDS` and
`PARAMETERIZED_KINDS` (18 distinct kinds, counted from `runs.py:39-51` and
`:98-179`) to one answer. The proposed table:

| Kind | Hosted | Needs | Why |
|---|---|---|---|
| `discovery` | runs | none up front | results are tracker rows; each source reports its own missing token (`sources/apify_linkedin.py:315`) |
| `demo` | runs | none | `demo-run` exercises the run machinery and touches no data (`harrier_cli/main.py:2202-2203`); spec 110's staging check uses it |
| `digest` | runs | `telegram`, unless `--dry-run` | it sends through `send_telegram_message` (`harrier/digest.py:22`, `:341`) |
| `check-feeds` | runs | none | reads the owner's boards from the store (`harrier_cli/main.py:1718-1734`) |
| `reconsider` | runs | none | results are tracker rows |
| `events-backfill` | runs | none | results are event rows |
| `evaluate-prospects` | runs | `ai` | calls `evaluate_offer` (`harrier/offers/batch.py:91`); results are tracker rows |
| `tailor` | 501 until spec 107 | | the result is a file (`resume/tailor.py:51`) |
| `cover-letter` | 501 until spec 107 | | a file (`apply/letters.py:197`) |
| `answers` | 501 until spec 107 | | a file (`apply/answers.py:169`) |
| `evaluate` | 501 until spec 107 | | a report file (`offers/evaluate.py:107`; `harrier_cli/main.py:631`) |
| `outreach-draft` | 501 until spec 107 | | a draft file (`outreach/drafts.py:122`) |
| `find-contacts` | 501 until spec 107 | | candidates are staged in a file (`outreach/discovery.py:45`), which approval reads |
| `backfill-posters` | 501 until spec 107 | | stages posters into the same candidates file (`outreach/backfill.py:1-8`, `:62-77`) |
| `scoring-export` | 501 until spec 107 | | an export file (`scoring/export.py:42`) |
| `gmail-watch` | 501, spec 109 | | Gmail is not offered hosted (spec 109) |
| `backup` | 501, local only | | spec 112's local-only class |
| `verify-backup` | 501, local only | | spec 112's local-only class; it checks an archive in the operator's backups directory (spec 096) |

A kind whose result is a file waits for spec 107. Running it now would
write a file no hosted route can serve, to a disk that is replaced on the
next deploy. The detail is `<kind> runs are not available on the hosted
API until spec 107`, `<kind> runs are not offered on the hosted service
(spec 109)`, or `<kind> runs are local only`. Locally the table is not
read.

### Starting a hosted run

`RunManager.start(kind, params, owner)` hosted, in this order, before it
writes an input file or spawns anything:

1. An empty owner is refused (spec 104).
2. A kind that does not run hosted is refused with its 501.
3. The lock key is `(owner, kind, target)`. A request repeats only its own
   owner's active run. Another owner's active run of the same kind and
   target is invisible to it, so it starts its own.
4. The route has read the owner's ciphertexts inside its request
   transaction, as the owner, and passes them to `start`. `start`
   decrypts them with the key. A kind's required credential that is unset
   is refused with 409 `this run needs your <kind> credential; set it
   under credentials`. A stored credential that does not decrypt is
   refused with 409 `your <kind> credential cannot be read; set it again`,
   and the log names the kind and key id, never a value or an owner's
   identity.
5. The child's environment is built (below).
6. The child is spawned with that environment, and with its owner's run
   directory as working directory.

Decrypted values live in the child's environment mapping and in the run's
scrub set, both in memory only. The journal records the owner (spec 104)
and never a value. The scrub set is dropped when the run reaches a terminal
state.

### The child's environment

`runenv.build_run_env(server_env, owner, run_dir, credentials)` returns the
whole environment. Nothing else reaches the child.

| Source | Names |
|---|---|
| Copied from the server's environment when set | `PATH`, `LANG`, `LC_ALL`, `TZ`, `PLAYWRIGHT_BROWSERS_PATH` (`Dockerfile:95`), `HARRIER_REVISION`, `HARRIER_BUILT_AT`, `HARRIER_LOG_LEVEL`, `APIFY_LINKEDIN_ACTOR`, `APIFY_LINKEDIN_PROFILE_SEARCH_ACTOR`, `APIFY_ACADEMIC_ACTOR`, `APIFY_ACADEMIC_PORTALS`, `ANTHROPIC_MAX_TOKENS` |
| Set by the run manager | `HOME` and `HARRIER_DATA_DIR`, both the owner's run directory; `HARRIER_DATABASE_URL`, the value of the API's `HARRIER_RUN_DATABASE_URL`; `HARRIER_RUN_OWNER`, the owner's id |
| From the owner's credentials | the names in the kinds table, for each kind the owner has set |

Every other name is absent. In particular: the API's own
`HARRIER_DATABASE_URL`, `HARRIER_RUN_DATABASE_URL` under its own name,
`HARRIER_CREDENTIALS_KEY`, `HARRIER_AUTH`, `HARRIER_SUPABASE_URL`,
`HARRIER_ALLOWED_HOSTS`, `HARRIER_CORS_ORIGINS`, every `FLY_` and
`SUPABASE_` name, every `_DEBUG` name (prompt logging is off in hosted
runs; spec 110 owns the hosted logging rules), `CLAUDE_CODE_OAUTH_TOKEN`,
`CLAUDE_CLI_PATH`, `CODEX_CLI_PATH`, `GMAIL_` names, and any operator
value of a credential name. The copied names were chosen from the 42
distinct variable-name literals in `services/api/src` (`grep -rhoE` for
the prefixes `HARRIER_`, `AI_`, `OPENAI_`, `ANTHROPIC_`, `CODEX_`,
`CLAUDE_`, `APIFY_`, `GMAIL_`, `TELEGRAM_`, `HUNTER_`, `PLAYWRIGHT_`,
`SUPABASE_`, `FLY_`, then `sort -u`), keeping only those that are
configuration and not a secret.

Grandchildren (Chromium, `pdftotext` in `resume/pdf.py:70`) inherit the
child's environment, which is already the allowlist.

### The owner's run directory

`<API data_dir()>/owners/<owner id>/`, created with mode 0700 on first use.
Every path a run builds under `data_dir()` is then under its owner's
directory with no domain change, because `data_dir()` honours
`HARRIER_DATA_DIR` (`db.py:35-38`). That qualifies by owner every file path
built from a track id (`academic/discovery.py:153`, `:172`,
`screening/seen.py:85`). Mode 0700 does not separate owners from each
other, since every run is the same user (The trust model); it keeps the
directory from other users of the machine.

The API's own inputs (`runs.py:302-332`) stay in the API's
`data_dir()/runs/inputs`, named by random id and removed when the run ends.
This directory is an interim. Spec 107 moves run files to Storage under
the owner's prefix and removes it.

### The run's Storage identity

Decision D3 (2026-10-11) puts this here. A hosted run's `files()` is the
store this spec's binding gives it, under its owner's prefix in
`harrier-files`, and never spec 107's local store, although the child's
environment carries no `HARRIER_AUTH`: `HARRIER_RUN_OWNER` is what makes a
run hosted. Spec 107 states why a database login that sets claims is not
enough: a Storage request needs a token Supabase accepts, with `sub` the
run's owner. The request's token expires, and a scheduled run (spec 108)
has none. Which token the binding gives the run is Open decision 8; until
it is taken, the binding is specified by what it guarantees, not by how.

### Run keys built from a track id

The run-outcome key `discovery:<track id>` (`academic/discovery.py:71`)
does not change. It is stored in `job_runs`, whose Postgres primary key is
`(owner_id, job)` since spec 105 (`schema.py:935`), and read under the
owner's policy (`runoutcome.py:138-161`). Owner A's `discovery:1` and owner
B's `discovery:1` are two rows. Writing the owner into the key would make
domain SQL name a tenant, which ADR-013 decision 2 forbids. The keys that
did need an owner are the in-memory lock key (above) and the file paths
(above).

### The run's database session

Spec 112's `open_store()` takes an optional `owner` (Changes to approved
specs). With one, it returns a run `Connection` over Postgres that:

1. connects with `HARRIER_DATABASE_URL`, which in a hosted run is the run
   login role;
2. begins every transaction with `SET LOCAL ROLE harrier_tenant` and
   `SELECT set_config('request.jwt.claims', <claims>, true)`, where the
   claims are `{"sub": "<owner>", "role": "authenticated", "aud":
   "authenticated"}`;
3. reads back `current_user` and the claims' `sub` in the same transaction
   and, when either differs, rolls back and raises `StoreDatabaseError`
   `the run's database identity did not hold`, before any domain
   statement;
4. gives a statement issued outside `transaction()` its own transaction
   with the same first steps, so no statement runs without the identity.

This is the request transaction of spec 104, applied per transaction
because a run has no request around it. `SET LOCAL` ends with the
transaction, so a connection behind a transaction-mode pooler carries no
owner into another client's transaction. The claims are built by the API
from the verified `sub` and are not a signed token: `auth.uid()` reads
`sub`, and `auth.jwt()` in a run returns these three claims, not the
request's payload.

### The CLI on a Postgres URL

Spec 112's gate gains this class and the operator class, written into
spec 112 as its Amendment (2026-10-11) (decision D2). Classes are by
command and subcommand, as spec 112's local-only list already is
(`config import`, `profile import`).

| Class | Commands | On a Postgres URL |
|---|---|---|
| `harrier store` (spec 103) | `store` and its subcommands | runs |
| operator (specs 108, 111) | `harrier operator` and its subcommands | runs; each subcommand runs as the database role it declares |
| local only (spec 112) | as spec 112 lists | refused, spec 112's text |
| run command, owner required (this spec) | the verbs of the kinds that run hosted: `discover`, `demo-run`, `digest`, `config check-feeds`, `reconsider`, `events backfill`, `evaluate-prospects` | runs only with `HARRIER_RUN_OWNER` set; otherwise refused |
| everything else (spec 112) | the rest | refused, spec 112's text |

The refusal: `error: harrier <command> runs on a Postgres store only as a
hosted run, which the API starts for one owner (spec 106)`, exit 1, before
anything opens. `<command>` is the command and subcommand words only (spec
061). A run verb whose kind later becomes hosted (spec 107, 109) moves into
this class in that spec, and the test that walks the parser fails until it
does.

When `HARRIER_RUN_OWNER` is set, `main`:

- does not call `load_project_env()`, so no `.env` is read;
- refuses a value that is not a UUID: `error: HARRIER_RUN_OWNER is not an
  owner id`, exit 1;
- refuses unless `HARRIER_DATABASE_URL` names a Postgres store: `error:
  HARRIER_RUN_OWNER is set but HARRIER_DATABASE_URL names no Postgres
  store`, exit 1. A stray variable cannot change a local command;
- refuses any command outside the run class with spec 112's text for its
  class;
- opens the store with `open_store(owner=...)`.

### Providers in a hosted run

When `HARRIER_RUN_OWNER` is set, `harrier.llm.config.load_config`:

- refuses `codex-cli` and `claude-cli` with `LLMClientError` `the CLI
  providers are local only (ADR-013 decision 8)`;
- resolves `auto` among `openai-api` and `anthropic-api` only, by which
  key is set, and raises `LLMClientError` `no AI credential is set for
  this account` when neither is.

The run manager always sets `AI_PROVIDER` from the owner's `ai` credential
when one exists, so `auto` is reached only when the owner has none. Locally
nothing changes.

### Log redaction in a hosted run

`_load_identity_values` loads the identity values through the run's own
`Connection` when `HARRIER_RUN_OWNER` is set, so the values are the owner's
and no SQLite file is opened. A read failure keeps today's warning that
logs are not redacted.

### The owner's values never reach the stream

`RunManager._append` already scrubs credential-shaped text
(`services/api/tests/test_api_exposure.py::test_every_event_is_scrubbed_at_the_choke_point`).
Hosted, it also replaces every exact occurrence of the run's secret field
values (spec 116's kinds table, "Secret fields") with `REDACTED`, in log
lines, structured events and the run's summary alike. The Telegram
`chat_id` is secret everywhere (decision D7, 2026-10-11) and is replaced
like `bot_token`. `provider` and `model` are not secret and are not
replaced.

### One login role for requests, another for runs

Hosted start-up requires `HARRIER_RUN_DATABASE_URL`, a Postgres URL
for a login role called `harrier_run` in the documents. The API applies
spec 104's login-role checks to it at start (not a superuser, no
`BYPASSRLS`, not a member of `service_role` or `postgres`, can `SET ROLE
harrier_tenant`, owns no table in `public`), naming `HARRIER_RUN_DATABASE_URL`
and never its value, and refuses when it names the same role as
`HARRIER_DATABASE_URL`. The deploy creates the role and grants it
`harrier_tenant` (spec 110).

A second role buys four things, none of them containment: a run never
holds the API's credential; `pg_stat_activity` tells runs from requests;
the operator can rotate either alone; and a `CONNECTION LIMIT` on
`harrier_run` keeps runs from taking every connection requests need.

### Where a run lives on Fly

The registry, the journal (`runs.py:575-577`) and a run's process are on
one machine. A request for a run reaches the right machine only if there
is one.

For the first hosted release exactly one machine serves the API. Spec
110 configures it and checks the machine count at deploy (Changes to
approved specs). This text first had that machine run with
`auto_stop_machines = "off"`; the hosting budget of 2026-10-11 replaced
that with auto-stop on, and "Hosting budget (2026-10-11)" below says how
a run keeps the machine up and how a run cut off by a stop is recorded.
A live run is held in memory on that machine; a run that ended, or was
cut off, is read from `run_records`.

A second machine needs `fly-replay` routing first (Options weighed), as
its own spec.

### ADR-013 decision 4, amended

The CLI bullet becomes:

> - CLI: the hosted deployment has no tenant CLI a person runs. A run
>   (ADR-004) is a `harrier_cli` process the API starts for one owner: it
>   receives the owner in `HARRIER_RUN_OWNER`, a login role that can only
>   become `harrier_tenant`, and an environment built from an allowlist,
>   and every transaction it opens runs as that owner. On a Postgres
>   store, a run command without an owner is refused, and every other
>   command is refused as spec 112 states. Operator commands are
>   subcommands of one group, `harrier operator`, and each declares the
>   database role it runs as (specs 108 and 111).
>
> **Amended (spec 106).** This said there is no tenant CLI, while every
> run is a CLI process. A run is bound to its owner by its parent, the API.
> Row-level policy then catches a bug in a run; it does not contain a
> compromised run, which can claim any owner (spec 106, The trust model).

Decision 8's note naming the key's location is spec 116's.

## Failure modes

- **A kind that does not run hosted.** 501 with its detail. No file, no
  process.
- **Two owners start the same kind and target.** Two runs. Neither sees
  the other's.
- **A required credential is unset.** 409, nothing spawned.
- **No `HARRIER_RUN_DATABASE_URL`, a non-Postgres one, one whose role
  fails a login-role check, or one naming the API's own role.** Start
  refused naming the variable, exit 1.
- **A run's identity does not hold** (a pooler that splits a transaction,
  a connection in autocommit). `StoreDatabaseError` before any domain
  statement; the run fails with that text.
- **A run command on a Postgres URL without `HARRIER_RUN_OWNER`.** Refused
  with the run class text, exit 1, nothing opened.
- **`HARRIER_RUN_OWNER` not a UUID, or set beside SQLite.** Refused, exit 1.
- **A `.env` file in a hosted run's working directory.** Not read.
- **A CLI provider, or `auto` with no AI credential, in a hosted run.**
  `LLMClientError`; the run reports it as today's provider errors are.
- **A provider error message quotes the owner's key.** Replaced with
  `REDACTED` before it reaches the stream.
- **The machine restarts, stops, or a deploy replaces it.** Runs in
  flight end with their process. The owner directories are on the
  machine's ephemeral disk and are gone. Each run's row in `run_records`
  stays and shows the run `interrupted` (Hosting budget (2026-10-11));
  this replaces "those runs are not listed afterwards", which this line
  said before the hosting budget.
- **A second API machine is started by hand.** Run requests reaching it
  answer 404. Spec 110's deploy check is the guard; nothing in this spec
  detects it.

## Acceptance criteria

- [ ] Every kind in `KIND_COMMANDS` and `PARAMETERIZED_KINDS` has one entry
      in `HOSTED_RUN_KINDS`, and a new kind without one fails the test
      (planned test_every_run_kind_declares_its_hosted_answer).
- [ ] Hosted, each kind refused in the table answers 501 with its detail,
      writes no input file and spawns nothing (planned
      test_a_kind_not_offered_hosted_answers_501_naming_its_spec). Spec
      104's planned refusal test for every hosted run is replaced by this
      one.
- [ ] Hosted, owner B starting a kind and target owner A has active gets a
      new run with a different id; A's request repeated joins A's run
      (planned test_two_owners_never_join_each_others_run).
- [ ] A run whose kind needs an unset credential is refused with 409 and
      spawns nothing (planned
      test_a_run_that_needs_a_missing_credential_is_refused_before_spawning).
- [ ] With a server environment holding a sentinel value under every name
      the Behavior section excludes, plus a random name, a hosted child's
      environment (captured by a child that prints its names and hashes of
      its values) holds exactly the allowlisted names, its owner's values,
      and no sentinel (planned
      test_the_child_environment_holds_only_the_allowlist_and_its_owners_values).
- [ ] `HARRIER_CREDENTIALS_KEY`, the API's `HARRIER_DATABASE_URL` value and
      another owner's credential never reach a child (planned
      test_the_credentials_key_never_reaches_a_run).
- [ ] A hosted run's working directory, `HOME` and `HARRIER_DATA_DIR` are
      its owner's run directory, mode 0700, and two owners' runs on track
      id 1 write seen-state to two different files (planned
      test_a_hosted_run_works_in_its_owners_directory).
- [ ] A hosted run with a `.env` file in its working directory does not
      load it (planned test_a_hosted_run_reads_no_env_file).
- [ ] On a run `Connection`, every transaction, including one opened by a
      bare statement, runs as `harrier_tenant` with the owner's `sub`, and
      a probe placed before the identity statements never runs (planned
      test_a_run_connection_sets_its_identity_in_every_transaction).
- [ ] A run connection whose identity read-back differs raises the
      identity error and runs no domain statement (planned
      test_a_lost_run_identity_stops_the_transaction).
- [ ] A run as owner A reads none of owner B's rows in any owned table
      (planned test_a_run_reads_only_its_owners_rows).
- [ ] Two owners each record `discovery:1` and both rows exist with their
      own success time (planned
      test_a_run_outcome_key_is_per_owner_without_naming_one).
- [ ] Every command and subcommand on a Postgres URL is in exactly one of
      the classes in the table under Behavior (spec 112's Amendment of
      2026-10-11); the run class refuses without
      `HARRIER_RUN_OWNER` with its text; `HARRIER_RUN_OWNER` that is not a
      UUID, or beside SQLite, is refused (planned
      test_a_run_command_needs_an_owner_on_postgres, and spec 112's
      planned test_the_postgres_gate_has_operator_and_run_classes).
- [ ] In a hosted run, `codex-cli` and `claude-cli` are refused, and `auto`
      never selects a CLI provider even with both binaries present
      (planned test_hosted_runs_offer_only_the_api_providers).
- [ ] A hosted run's log redaction loads its owner's identity values from
      Postgres and creates no SQLite file (planned
      test_a_hosted_run_redacts_logs_from_its_own_store).
- [ ] A child that prints its owner's API key in a log line and in a
      structured event produces events holding `REDACTED` and not the key
      (planned test_a_credential_value_never_reaches_the_event_stream).
- [ ] A child that prints its owner's Telegram chat id in a log line, a
      structured event and its summary produces events and a summary
      holding `REDACTED` and not the chat id (decision D7; planned
      test_a_telegram_chat_id_never_reaches_the_event_stream).
- [ ] In a hosted run, `files()` is bound to the run's owner before any
      domain code runs, writes only under that owner's prefix, and never
      returns spec 107's local store (decision D3; planned
      test_a_hosted_run_binds_its_owners_storage).
- [ ] Hosted start refuses a missing or non-Postgres
      `HARRIER_RUN_DATABASE_URL`, one whose role fails a login-role check,
      and one naming the API's own role (planned
      test_hosted_start_checks_the_run_login_role).
- [ ] Locally, with `HARRIER_AUTH` and `HARRIER_DATABASE_URL` unset, every
      existing run test passes unchanged, including
      `services/api/tests/test_runs.py::test_journal_survives_restart`.

Documents and gates:

- [ ] ADR-013 decision 4 carries the "Amended (spec 106)" note.
- [ ] `docs/architecture.md`'s hosted section states the trust model and
      names `runenv.py` and this spec.
- [ ] Once approved, the specs named under "Changes to approved specs"
      carry those lines, and nothing else in them changes.
- [ ] CI's `check-python` job runs the new Postgres tests, not skipped.
- [ ] `uv run ruff check`, `uv run pyright`, `pnpm type-check` and `pnpm
      lint` pass.
- [ ] `just check` passes.

## Honest limitations

- **A compromised run is not contained.** It can claim any owner in the
  database and, on one machine as one user, can likely read other runs'
  environments and the API's (The trust model). The guarantee is against
  bugs.
- **The API process is the single point that holds everything.** Its
  compromise exposes every owner's credentials and rows. Splitting the key
  from the data protects only against a database-side breach.
- **Assumptions for spec 110's staging check,** each unconfirmed here:
  that a process of the same user can read `/proc/<pid>/environ` of the
  API and of sibling runs in Fly's machine image (proc_pid_environ(5)
  names the ptrace check, not its result there); that Supabase's pooler,
  if the run URL goes through it, keeps `SET LOCAL` within one
  transaction; and that a rolling deploy of the API app replaces its one
  machine, ending runs in flight. The assumption that
  `auto_stop_machines = "off"` leaves a machine running is superseded by
  the hosting budget (2026-10-11); its replacements are spec 115's F1 and
  F2.
- **Hosted run files are lost on every stop.** The owner directories are
  on the machine's root disk, which Fly resets on restart and redeploy,
  and, with auto-stop on, at each start after an idle stop (spec 115's
  F2). Spec 107 moves run files to Storage. Until then, discovery's
  seen-state is relearned after each stop; the tracker's per-owner URL
  key still refuses a duplicate. Run history is kept in `run_records`
  since the hosting budget, not in the journal file.
- **Half the run kinds wait for spec 107.** Those whose result is a file
  answer 501 hosted.
- **The claims in a run are not a token.** `auth.jwt()` in a run holds
  three claims, not the payload a request carries. Nothing under
  `services/api/src` or `services/api/tests` names `auth.jwt` today
  (`grep -rn "auth.jwt"`).
- **A long run outlives the token that started it.** The run acts as its
  owner until it ends, though the owner may have signed out. The run
  manager's cancel is the only stop.
- **The run's own log file** under the owner's directory is identity
  redacted and scrubbed by pattern, not by the owner's exact secret values.
- **A chat id is a number.** Replacing its exact occurrences also
  replaces an unrelated number in an event that contains the same digits.
  The scrub prefers a redacted number to a leaked chat id (decision D7).
- **The run's Storage token is not chosen yet** (Open decision 8). Until
  it is, a hosted run kind that writes files through spec 107's seam,
  discovery's seen state included, cannot run once spec 107 binds hosted
  `files()`.
- The line numbers cited here are refreshed by the change that implements
  this spec, not before (decision D9, 2026-10-11).

## Migration

None for the local product. No variable it reads changes meaning, and
runs inherit their environment as before. The `credentials` table is spec
116's. Since the hosting budget (2026-10-11) this spec adds one
Postgres-only migration, `run_records`, declared Postgres-only in
`SINGLE_DIALECT_MIGRATIONS`, which records the version on SQLite and
changes nothing there.

A hosted deploy needs, before this spec's API starts: the `harrier_run`
login role granted `harrier_tenant`, `HARRIER_RUN_DATABASE_URL` as a Fly
secret, spec 116's `HARRIER_CREDENTIALS_KEY`, the store at the version
that creates `run_records`, and one API machine with auto-stop on as spec
110's "Hosting budget (2026-10-11)" sets it.

## Changes to approved specs

Each line below is a change this spec asks of an approved spec. None is
written into that spec here; each is an amendment Akin approves with this
spec.

- **Spec 104:** hosted `RunManager.start` no longer refuses every run with
  `RunsNotHosted`; each kind answers as `HOSTED_RUN_KINDS` says (Behavior,
  "Which kinds run hosted"). Its planned refusal test for every hosted run
  is replaced by
  planned test_a_kind_not_offered_hosted_answers_501_naming_its_spec.
  Hosted start-up also requires `HARRIER_RUN_DATABASE_URL`, checked with
  its login-role checks, and refuses one naming the API's own role.
- **Spec 112:** `open_store()` takes an optional `owner` and returns a run
  `Connection` that sets the owner's identity in every transaction
  (Behavior, "The run's database session"). The CLI's Postgres refusal
  classes gain a third, "run command, owner required" (Behavior, "The CLI
  on a Postgres URL"), and its
  planned test_every_command_has_a_postgres_refusal_class covers the third
  class. Written into spec 112 as its Amendment (2026-10-11), with the
  operator class of specs 108 and 111 (decision D2), and its planned
  test_the_postgres_gate_has_operator_and_run_classes.
- **Spec 107:** run files move to Storage under the owner's prefix, and the
  owner run directory (Behavior, "The owner's run directory") goes. Spec
  107's refined text of 2026-10-10 leaves a run's Storage identity to this
  spec. Decision D3 (2026-10-11) settles it: the identity is this spec's
  (Scope item 12, Behavior, "The run's Storage identity"), and spec 107
  consumes the binding.
- **Spec 110:** one API machine with `auto_stop_machines = "off"` and a
  machine count check at deploy; the `harrier_run` login role, granted
  `harrier_tenant`; `HARRIER_RUN_DATABASE_URL` as a Fly secret (Migration).
  Written into spec 110's Amendment (2026-10-11) (decision D4), with the
  staging assumptions under Honest limitations. The hosting budget of
  2026-10-11 replaced `"off"` with auto-stop on; spec 110's "Hosting
  budget (2026-10-11)" carries that, and the machine count check stays.
- **Spec 104 (hosting budget):** hosted `GET /runs` and `GET
  /runs/{run_id}` read a run that is not live on this machine from
  `run_records` under the owner's policy, so they show an interrupted run
  instead of answering 404 for it.
- **Spec 105 (hosting budget):** the catalog gains `run_records` as an
  owned, policed, hosted-only table. `OWNED_TABLES` gains it, migration
  10 keeps its frozen list, and `HOSTED_ONLY_TABLES` declares it. Specs
  108 and 116 ask the same freeze and declaration; whichever lands first
  adds them.
- **Spec 114:** the SPA's credentials page for spec 116's routes. Written
  into spec 114's Amendment (2026-10-11) (decision D5).

## Options weighed

- **The run reuses the API's login URL.** One role fewer. The run would
  hold the credential requests use, and runs could take every request
  connection. Not chosen (Open decision 3).
- **Session-level role and claims on the run connection.** One statement
  per connection instead of two per transaction. Behind a transaction-mode
  pooler the session state would stay on the server connection and reach
  another client's transactions with this owner's claims. Not chosen.
- **The run talks to the database through the API.** The run would hold
  no database credential at all, and the API, which verified the owner,
  would be the only place claims are set. Every domain call in a run would
  become a remote call, which is a rewrite of the run model (ADR-004). Not
  chosen; it is the direction that would contain a compromised run's
  database reach.
- **A database credential that can claim one owner only.** For example, a
  policy for runs that reads a run grant row keyed by the backend process,
  set by the API. It needs a second policy per owned table, which spec
  105's catalog test refuses, and a definer function. Not chosen.
- **A separate operating system user, or a Fly machine, per run.** Would
  stop runs reading each other's environments. The first needs a
  privileged launcher in the image; the second, the Fly Machines API and a
  start latency per run. Not chosen for the first hosted release.
- **Lift the 501 for every kind now, with files on the machine.** Tailor
  and its siblings would succeed and write files no hosted route serves,
  on a disk the next deploy erases. Not chosen: a kind is offered when its
  result can reach its owner.
- **An allowlist locally too.** Locally the child needs the operator's own
  `CLAUDE_CODE_OAUTH_TOKEN`, CLI paths and `.env`, and spec 035 documents
  the inheritance. Not chosen: local behavior does not change.
- **`fly-replay` routing with several machines.** Run ids would carry
  `FLY_MACHINE_ID`, and a run route for another machine's id would answer
  with `fly-replay: instance=<id>`. Fly documents the header, a 1 MB limit
  on replayed requests and the need for the http handler; the page says
  nothing about server-sent events through a replay. Starting a run would
  still land on any machine, and auto-stop would still need to spare busy
  machines. Not chosen for the first release (Open decision 5).
- **Qualify `discovery:<track id>` with the owner in the string.** The
  `job_runs` key is already per owner on Postgres, and the string would
  make domain SQL name a tenant. Not chosen.

## Decisions taken (2026-10-11)

Akin took these in chat on 2026-10-11 ("go with your recommendations").
The text above says where each changed it.

- **D2, one operator group.** `harrier operator` is one command group, and
  each subcommand declares the database role it runs as. Spec 112's gate
  gains exactly two classes beyond `harrier store`: operator, and this
  spec's "run command, owner required". The CLI table, Scope item 5 and
  the ADR text under Behavior say so.
- **D3, a run's Storage identity is this spec's.** Binding a run to its
  owner includes the identity the run uses for Storage; spec 107 consumes
  the binding and does not define it (Scope item 12, Behavior, "The run's
  Storage identity", Open decision 8).
- **D4, spec 110 takes this spec's asks.** The spec 110 line under
  "Changes to approved specs" and the staging assumptions under Honest
  limitations are written into spec 110's Amendment (2026-10-11).
- **D5, the credentials page.** Spec 114 gains it by amendment.
- **D7, the Telegram chat id is secret everywhere.** This spec scrubs it
  from run events and summaries (Behavior, "The owner's values never reach
  the stream").
- **D9, line numbers.** Refreshed by this spec's implementing change
  (Honest limitations).

## Open decisions for Akin

1. **Split.** Applied in this text: the credential store is spec 116,
   landing first, and this spec depends on it. The repository's rule is to
   split the spec, not the branch. Alternative: one spec, two pull
   requests in order, as spec 112 was delivered.
2. **Where the key lives.** Moved to spec 116 (its Open decision 2).
3. **The run's login role.** Recommended: a second role, `harrier_run`,
   passed to the child as its `HARRIER_DATABASE_URL`. Alternative: the
   API's own login URL, one role fewer.
4. **Which kinds run hosted first.** Recommended: the seven in the table
   whose results are rows, a notification or nothing; the eight whose
   result is a file wait for spec 107. Alternative: all of them now, with
   files on the machine's disk until spec 107.
5. **Run routing on Fly.** Recommended: one API machine with auto-stop off
   for the first hosted release, configured and checked by spec 110.
   Alternative: `fly-replay` by machine id in run ids, its own spec,
   needed before a second machine.
   Superseded 2026-10-11 by the hosting budget: one API machine stays and
   `fly-replay` stays out, but auto-stop is on. Open decisions 9 and 10
   take the two questions that raises.
6. **The hosted-only table.** Moved to spec 116 (its Open decision 3).
7. **Containing a compromised run.** Recommended: state the limit now, and
   leave containment (a user or machine per run, and a database path that
   cannot claim another owner) to a later spec before open sign-up.
   Alternative: block this spec on it, which keeps every hosted run kind at
   501 until then.
8. **The token a run uses for Storage.** Added 2026-10-11, when decision
   D3 placed a run's Storage identity in this spec. Spec 107 names the two
   ways it found: forward and refresh the owner's own session, which a
   scheduled run (spec 108) does not have; or a signing key harrier holds,
   imported into the project, that mints short-lived tokens for the run's
   owner, which gives the holder of that key the power to act as any owner
   in Storage. No recommendation is made in this text. Until this is
   taken, Behavior, "The run's Storage identity" states the guarantee only.
9. **Keeping the machine awake during a run.** Added 2026-10-11 by the
   Taken 2026-10-11 (Akin, "go with your recommendations"): the recommended answer.
   hosting budget. Recommended: while any run is active, the run manager
   sends `GET /health` to the API's own public hostname, through Fly's
   proxy, at a fixed interval shorter than the proxy's stop loop, and
   stops when the last run ends (Hosting budget, "Keeping the machine
   awake"). It works whether or not a browser is watching. Whether the
   proxy counts it as load is spec 115's F1. Alternatives: hold one
   request to the API's own hostname open while runs are active, which
   puts a connection on the machine at every pass of the stop loop; or
   Fly's documented "exit when idle" pattern, `auto_stop_machines =
   "off"` with `auto_start_machines = true` and the API exiting itself
   when no run is active and no request has arrived for a while, which
   needs no assumption about the proxy but changes spec 110's setting.
10. **Where an interrupted run is recorded.** Added 2026-10-11 by the
   Taken 2026-10-11 (Akin, "go with your recommendations"): the recommended answer.
    hosting budget. Recommended: `run_records`, a hosted-only owned table
    in Supabase, written as the owner, with each run's API process id, so
    a row left `queued` or `running` by an earlier process reads as
    `interrupted` (Hosting budget, "A run cut off by a stop"). Alternatives:
    the journal on a Fly volume, which costs $0.15/GB a month, ties runs
    to one machine, and is ruled out by spec 110's no-volume rule; or the
    run lost with the machine's disk, as this text said before, which the
    hosting budget forbids.

## Proof / origin

- `docs/adr/ADR-013-hosted-multi-tenant-deployment.md`, decisions 2, 3, 4
  and 8, and its Honest limitations on the service role.
- `specs/102-hosted-multi-tenant-deployment.md`, open decision 3, and the
  Local-first bullet it amended.
- `specs/104-the-hosted-api-authenticates-every-request.md`: the request
  transaction, the login-role checks, `Run.owner`, and the 501 this spec
  replaces.
- `specs/105-every-personal-table-is-owned-and-policed.md`: the owned-table
  shape, the catalog test, the `harrier_tenant` role (Amendment), and the
  routed finding on track ids.
- `specs/116-tenant-credentials-are-stored-encrypted.md`: the credential
  store this spec reads, split from this spec.
- `specs/112-the-tracker-domain-runs-on-either-store.md`: `Connection`,
  `open_store()`, the upsert shape and the CLI refusal classes.
- `specs/035-local-api-exposure.md`: the inherited environment, the scrub
  at the choke point.
- `specs/047-apply-page.md` (runs that take an argument) and
  `services/api/src/harrier_api/runs.py`: kinds, targets, joining, the
  journal and the spawn.
- `services/api/src/harrier/llm/config.py` and `providers.py`; `notify.py`;
  `sources/apify_linkedin.py`, `sources/apify_academic.py`;
  `outreach/discovery.py`, `outreach/hunter.py`; `mail/watch.py`: where
  each credential is read.
- `services/api/src/harrier_cli/main.py`, `harrier/logsetup.py`,
  `harrier/db.py`, `harrier/academic/discovery.py`,
  `harrier/screening/seen.py`, `harrier/runoutcome.py`: every site listed
  under Problem and Behavior.
- `.env.example`, `.dockerignore`, `Dockerfile`.
- The post-merge review of PRs #194 and #207 (2026-10-10), architecture 1,
  8 and 10 and privacy 4.
- Fly.io, secrets: an encrypted vault per app, decrypted by the host agent
  and injected as environment variables at boot; setting a secret restarts
  machines and resets their ephemeral file system.
  https://docs.fly.io/apps/secrets, read 2026-10-10.
- Fly.io, autostop and autostart: `auto_stop_machines` `"off"`, `"stop"`
  or `"suspend"`; with `"off"` the proxy never stops machines;
  `min_machines_running` applies only when stopping is on.
  https://docs.fly.io/launch/autostop-autostart, read 2026-10-10.
- Fly.io, dynamic request routing: `fly-replay: instance=<id>`, the 1 MB
  replay limit, the http handler requirement.
  https://docs.fly.io/networking/dynamic-request-routing, read 2026-10-10.
- Fly.io, the machine runtime environment: `FLY_MACHINE_ID`.
  https://docs.fly.io/machines/runtime-environment, read 2026-10-10.
- Fly.io, volumes overview: the root file system is for temporary data
  rebuilt on deploy or restart. https://fly.io/docs/volumes/overview, read
  2026-10-10.
- Linux, proc_pid_environ(5): access governed by a
  `PTRACE_MODE_READ_FSCREDS` check.
  https://www.man7.org/linux/man-pages/man5/proc_pid_environ.5.html, read
  2026-10-10 through search results; the page itself did not load.

## Out of scope

- Gmail and Telegram setup flows, Telegram's route and check, and Gmail as
  a credential kind: spec 109.
- The credential store, its key, its routes and key rotation: spec 116.
- Operator-paid LLM calls.
- Files and run state in Storage, and lifting the 501 for kinds whose
  result is a file: spec 107.
- Scheduled runs: spec 108 starts them through this spec's run start.
- The Fly app, machine count, auto-stop setting, secrets and the
  `harrier_run` role's creation: spec 110.
- The SPA's credentials page: spec 114's Amendment (2026-10-11).
- Containing a compromised run (Open decision 7).
- Rate limits and quotas per owner.

## Hosting budget (2026-10-11)

Akin decided in chat on 2026-10-11 that the hosted deployment targets
under 5 USD per month, with ADR-013's topology kept: the API and its runs
stay on Fly. Spec 110's "Hosting budget (2026-10-11)" holds the prices
and the arithmetic. One shared-cpu-1x 1GB machine always on in Amsterdam
costs $6.95 a month (Fly, Pricing, read 2026-10-11: 6.70 × 1.038461538 =
6.96, shown by the page's region selector as $6.95), over the budget, so
the API's one machine runs with `auto_stop_machines = "stop"` and
`min_machines_running = 0`. Fly's proxy "stops Machines when the app has
excess capacity", in a loop that "runs every few minutes" (Fly,
Autostop/autostart, read 2026-10-11). Fly's page does not say whether an
open connection keeps a machine running, and does not mention
background work.

Region: Fly `ams` (Amsterdam) and Supabase `eu-west-1` (Ireland) since
2026-10-11, replacing Frankfurt; spec 110's "Region (2026-10-11)" holds
the decision.

A run is background work: once the browser closes its stream, nothing
the proxy sees says the machine is busy. The run registry, the live
events and the run's process are on that machine. So this spec now
states two things it did not: the machine is kept up while a run is
active, and a run cut off by a stop anyway is recorded. One machine
stays, and `fly-replay` stays out (Open decision 5, superseded).

### Keeping the machine awake

Hosted, while any run is `queued` or `running`, the run manager sends
`GET /health` to the API's own public hostname, the first host in
`HARRIER_ALLOWED_HOSTS`, so the request passes through Fly's proxy to
this machine. It sends one when the first run becomes active and then at
a fixed interval, a constant in the code chosen shorter than the proxy's
stop loop. Fly gives that loop only as "every few minutes", so the
constant's value is confirmed by spec 115's F1, not asserted here. The
beats stop when the last active run reaches a terminal state, and the
proxy may then stop the machine as usual.

- A beat that fails or times out is logged with its status class and no
  URL, and the next beat is sent at the interval. A failed beat never
  fails a run.
- Locally nothing is sent.
- A scheduled run (spec 108) runs on the scheduler machine, not here, and
  needs none of this.
- Whether the proxy counts these requests as load is an assumption (spec
  115's F1). If F1 refutes it, Open decision 9's alternatives are next,
  and this section is amended before the hosted run kinds are offered.

### A run cut off by a stop

The run journal is a file on the machine's root disk (`runs.py:575-577`),
which a start after an idle stop resets (spec 115's F2). Hosted, the
record lives in Supabase instead, in a new hosted-only owned table:

```sql
CREATE TABLE run_records (
    owner_id uuid NOT NULL DEFAULT auth.uid() REFERENCES auth.users (id),
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN
        ('queued', 'running', 'succeeded', 'failed', 'cancelled',
         'interrupted')),
    api_instance TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    exit_code INTEGER,
    PRIMARY KEY (owner_id, run_id)
);
```

- The states are `RunState`'s (`runs.py:35`). Timestamps are ISO 8601 UTC
  text, as `schedule_state` (spec 108) and `job_runs` use.
- Row security is enabled and forced with the `owner_only` policy every
  owned table has (spec 105, Amendment); the primary key leads with
  `owner_id`. `harrier_tenant` gets `SELECT, INSERT, UPDATE`; no grant
  includes `DELETE` or `TRUNCATE`, since deleting an owner's rows is spec
  111's. `PUBLIC`, `anon`, `authenticated` and `service_role` get
  nothing. It is declared in `HOSTED_ONLY_TABLES`: "per-owner run
  records (spec 106); the journal file keeps the local equivalent".
- It holds the run's id, kind, target, state, times and exit code. No
  parameters, no event and no output. The target is a track slug or a
  date for the kinds that run hosted.
- `api_instance` is a random UUID the API draws once at each process
  start and writes nowhere else.

Writes, each as the run's owner:

1. **Start.** `RunManager.start` inserts the row in the route's request
   transaction, before it spawns anything.
2. **Each state change.** The run manager opens a transaction on the
   API's login with `SET LOCAL ROLE harrier_tenant` and the run's owner's
   claims, read back as spec 104's request transaction does, and updates
   the row `WHERE run_id = %s AND api_instance = %s`. The owner is the one
   `start` recorded from a verified `sub`; this is the reach the API
   already has (The trust model).
3. **The stop signal.** When the API receives the signal Fly sends before
   it stops the machine, it records every active run `interrupted`, each
   in a transaction as its owner, then terminates the children with the
   run manager's grace (`runs.py:567`). Whether an auto-stop sends that
   signal and waits is spec 115's F2.
4. **A hard stop.** If the process ends with no chance to write, rows stay
   `queued` or `running` with the earlier `api_instance`. From the next
   start on, the first time the owner's request reads such a row through
   `GET /runs` or `GET /runs/{run_id}`, it is updated to `interrupted`
   with `ended_at` set, in that request's transaction, and shown so. The
   API runs no sweep across owners at start: it holds no role that writes
   another owner's rows outside a transaction as that owner, and gains
   none here (ADR-013 decision 3).

So a run cut off by a stop is never silently lost: its owner sees it
`interrupted`, as spec 041 names a run whose process disappeared, and it
is not a failure. Its events are not kept; the events route answers for a
run that is not live on this machine as it does today for an unknown id.
Hosted, the run manager writes no journal file. Locally the journal is
unchanged and `run_records` is not read.

### Acceptance criteria (hosting budget)

- [ ] Hosted, with a fake clock and a fake HTTP client, the first active
      run sends `GET /health` to the first host in
      `HARRIER_ALLOWED_HOSTS`, beats repeat at the interval while any run
      is active, and stop after the last run ends; a failed beat fails no
      run; locally nothing is sent (planned
      test_an_active_run_keeps_the_machine_awake).
- [ ] The new Postgres-only migration creates `run_records` as under
      "A run cut off by a stop", declared in `SINGLE_DIALECT_MIGRATIONS`
      and `HOSTED_ONLY_TABLES`, and
      `services/api/tests/test_owner_policy.py::test_every_table_is_owned_or_declared`
      and
      `services/api/tests/test_owner_policy.py::test_owner_a_reads_nothing_of_owner_b`
      pass with it.
- [ ] A hosted run's row is inserted at start and follows each state
      change as its owner; a row left `running` by another
      `api_instance` reads and is stored `interrupted` on its owner's
      next `GET /runs`, and another owner's such row is untouched
      (planned test_a_run_cut_off_by_a_stop_reads_interrupted).
- [ ] On the stop signal, every active hosted run is recorded
      `interrupted` as its owner before the process exits (planned
      test_the_stop_signal_records_active_runs_interrupted).
- [ ] Locally, `services/api/tests/test_runs.py::test_journal_survives_restart`
      passes unchanged and nothing reads or writes `run_records`.
- [ ] Spec 115's F1 and F2 are recorded confirmed or refuted before a
      hosted run kind is offered, and a refuted one amends this section
      first.

### Honest limitations (hosting budget)

- Keeping the machine awake rests on an assumption about Fly's proxy
  (F1). Fly documents excess capacity by `soft_limit`, not traffic, and
  says nothing about background work.
- A run keeps the machine up for its whole length, so run time is machine
  time against the budget. Spec 110's cost check sees it; nothing here
  caps it beyond each kind's own limits.
- `/health` is public. Anyone polling it keeps the machine awake and
  spends the budget. That was already true of any request once
  `auto_start_machines` is on; this section does not add a guard.
- A hard stop leaves a row `running` until its owner next reads the run
  list, so the row is stale in the database, though never shown as live
  once the process that owned it is gone.
- Run events are lost with the machine, as before.

### Proof / origin (hosting budget)

- Fly, Pricing: shared-cpu-1x 1GB in the base region $6.70 a month for
  30 days of continuous runtime, times Amsterdam's region multiplier
  1.038461538, shown as $6.95; a stopped Machine billed only for its root file
  system; volumes $0.15/GB a month. Read 2026-10-11.
  https://docs.fly.io/about/pricing
- Fly, Autostop/autostart: stops on excess capacity by `soft_limit`; the
  stop loop "runs every few minutes"; `min_machines_running` only with
  stopping on; the "exit when idle" pattern with `auto_stop_machines =
  "off"` and `auto_start_machines = true`; nothing on open connections
  or background work. Read 2026-10-11.
  https://docs.fly.io/launch/autostop-autostart
