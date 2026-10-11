---
spec: 110
title: Staging and production deploy one image after migrating their store
status: accepted
approved: yes
milestone: M10
depends: [103, 104, 105, 112, 114, 115]
---

# Spec 110: Staging and production deploy one image after migrating their store

Akin approved the stub in chat on 2026-10-10; the frontmatter flip reached main inside PR #222, whose message does not say so, and this line records it. Refined 2026-10-10 after approval of the stub. This text is an amendment
to the approved stub and goes to Akin before implementation.

## Problem

No hosted environment exists. ADR-013 decision 7 names the topology: Vercel
serves the SPA, Fly runs the API image, Supabase holds the database, auth
and storage, in one jurisdiction. Akin chose two environments, staging and
production, each with its own Supabase project, Fly app and Vercel
environment (spec 102, open decision 4), in the EU (open decision 2). The
exact regions were left to this spec.

Four approved specs hand this spec work:

- Spec 103 leaves "who runs `harrier store migrate` in production" and the
  Supabase project's Postgres major version here (Out of scope, Honest
  limitations). Its Honest limitations also name an open hole: a
  connection error's traceback holds the frame of `postgres_connect`,
  whose `url` argument carries the password
  (`services/api/src/harrier/pgstore.py:64-81`). "An error reporter that
  records local variables would see the password. None is configured;
  spec 110 decides the hosted one."
- Spec 104 refuses to start the hosted API when the store is not at
  `target_version()`, and says that refusal is "what enforces spec 110's
  migrations apply before a new API version serves". It leaves here the
  creation of the API's login role, the Fly secrets, the health check's
  Host header, and making the project's current signing key asymmetric
  (its Migration and Out of scope).
- Spec 105 leaves here the Data API exposure of harrier's tables (its open
  decision 6). It also left here a run of its isolation tests against a
  real Supabase project (its Honest limitations: "The shim is not
  Supabase"), which is now spec 115's.
- Spec 112 leaves here connection pooling, Supabase's pooler, and the
  runtime image installing psycopg (its Out of scope).

The post-merge reviews of PRs #194, #207 and #216 (2026-10-10) routed these
findings here, each kept below with its own criterion:

- **Spec 105 is proven only against a shim.** Akin decided on 2026-10-10
  that a first slice, which migrates a real Supabase staging project to
  the latest version and runs spec 105's isolation tests there, lands
  before spec 112. That slice was this spec's Part A and is now spec 115,
  split out on 2026-10-10. This spec depends on it. (architecture 3, data
  integrity 5)
- **New paths need a class first.** `.gitignore` ignores `.env` and every
  `.env.*` but `.env.example` (`.gitignore:2-4`), and so does
  `.dockerignore:5-7`, but `config/data-classification.json` lists only
  `.env` and `.env.local` (`:6-7`). Nothing classifies the files a Vercel
  or Fly CLI writes into a checkout. (privacy 5)
- **Hosted logs leave the machine.** Fly collects the API's standard
  output. Today `configure_logging` redacts by literal value, loading the
  values from one person's SQLite file
  (`services/api/src/harrier/logsetup.py:89-91`, `:124-140`), and writes a
  rotating file under `data/logs` (`logsetup.py:107-118`). Neither fits a
  process that serves many owners from Postgres. (privacy 5)
- **`.env` reaches the container.** `docker-compose.yml:51-53` loads `.env`
  with `env_file`, so a hosted store URL written there becomes a production
  credential in the local container's environment, and every run
  subprocess inherits it. The CLI and the API also read
  `HARRIER_DATABASE_URL` from `.env` when it is not exported
  (`store_target`, `connection.py:228-234` since spec 112's PR 1 moved it
  out of `pgstore.py` on 2026-10-11; spec 103 Corrections). (privacy 5)
- **Tracebacks hold the URL.** Above. (test-integrity follow-up)
- **The grant is nobody's step yet.** Migration 10 creates `harrier_tenant`
  with no member (`schema.py:849-864`). Spec 104's API connects as a login
  role that must be a member, and nothing grants it. (review of PR #216)

## Scope

The first slice, which runs spec 105's isolation tests on a real
Supabase project, was this spec's Part A. It is spec 115 since
2026-10-10 (Open decision 1). This spec is the deploy pipeline, built
after specs 104, 112, 114 and 115.

1. **A hosted image.** A `hosted` build target in `Dockerfile`, the local
   image staying the default target with unchanged behavior (open decision
   3).
2. **Fly configuration.** `deploy/fly.staging.toml` and
   `deploy/fly.production.toml`: app name, `primary_region = "ams"`, the
   HTTP service, the health check, the non-secret hosted variables in
   `[env]`, and the machine size and auto-stop settings under "Hosting
   budget (2026-10-11)".
3. **Vercel configuration.** `apps/web/vercel.json` turns off Vercel's own
   Git deployments, so only the workflow deploys, and serves the SPA's
   routes. Both Vercel projects are on the free Hobby plan (Hosting
   budget).
4. **`harrier store grant-tenant <role>`.** A new subcommand under
   `harrier store` that checks a login role and grants it `harrier_tenant`
   (Behavior).
5. **`harrier store check-deploy`.** A new subcommand that checks, as the
   migrator, what a deploy must find before the new API serves
   (Behavior).
6. **`.github/workflows/deploy.yml`.** Staging deploys on every push to
   `main` that passed CI; production deploys a commit staging already
   ran, by hand, after Akin's approval (Behavior). A guarded path.
7. **Classification before the files exist.** `config/data-classification.json`,
   `.gitignore` and `.dockerignore` gain the entries under Behavior.
   Guarded paths.
8. **The local container never takes a hosted store.** `docker-compose.yml`'s
   `environment:` sets `HARRIER_DATABASE_URL` and `HARRIER_AUTH` empty, so
   `.env` cannot reach the container with either (Behavior).
9. **A hosted URL in `.env` is refused** by `store_target` (open decision
   5).
10. **Hosted logging.** `configure_logging` in hosted mode opens no SQLite,
    writes no file, and renders records without their text arguments
    (open decision 6). The hosted API refuses to start with prompt logging
    on.
11. **No error reporter.** None is installed, and a test fails if one
    appears; adding one is its own spec, which must turn local variables
    off.
12. **Documents.** A runbook, `docs/deploy.md`, for the one-time setup of
    each environment and for a deploy that fails halfway;
    `docs/architecture.md`'s hosted section; `.env.example` comments.

## Behavior

### environments

| | Staging | Production |
|---|---|---|
| Supabase project | the existing Supabase project named `harrier`, `eu-west-1` | `harrier-production`, `eu-west-1`, not created yet |
| Fly app | `harrier-api-staging`, `ams` | `harrier-api-production`, `ams` |
| Vercel | project `harrier-web-staging`, its Production environment | project `harrier-web-production`, its Production environment |
| GitHub environment | `staging`, deployments from `main` only | `production`, deployments from `main` only, Akin as required reviewer |

Supabase in Ireland (`eu-west-1`) and Fly in Amsterdam (`ams`), as
"Region (2026-10-11)" at the end of this spec decides. Fly has no Dublin
region, and London (`lhr`) is outside the EU; Amsterdam is the closest
Fly region to Ireland inside it. Supabase warns that its generic "Europe"
choice may land in London or Zurich, so a named region is chosen. The API
and the database are in two cities, so every request's database round
trips cross between them. No latency figure is claimed.

Staging is the existing Supabase project named `harrier`. Production's
project does not exist yet: it is created when a Supabase Free slot is
free or the plan changes (Region (2026-10-11)). Nothing serves tenants
until then.

The names are the shape, not a commitment; the operator may pick others,
and the Fly configuration files carry whichever are chosen.

Each Supabase project is set up once, by hand, and the runbook records
each setting with the check that proves it:

- Data API off ("Enable Data API" in the dashboard). Supabase documents
  that with it off "none of the auto-generated REST endpoints respond,
  regardless of grants or RLS". Spec 105's grants already give `anon` and
  `authenticated` nothing; this closes the path a second time and spec
  105's open decision 6 asked for it.
- An asymmetric signing key current (spec 104 refuses `HS256`).
- Anonymous sign-ins off, and public sign-up off until spec 111 (spec 102,
  open decision 5).

### where each secret lives

| Value | Lives in | Never in |
|---|---|---|
| The `postgres` role's pooler URL (the migrator) | GitHub environment secret `HARRIER_MIGRATE_DATABASE_URL` | Fly, Vercel, `.env`, git |
| The API login role's URL | Fly secret `HARRIER_DATABASE_URL`, set once by the operator with `fly secrets set` | GitHub, Vercel, `.env`, git |
| A Fly deploy token for one app | GitHub environment secret `FLY_API_TOKEN` | Fly, Vercel, git |
| A Vercel token | GitHub environment secret `VERCEL_TOKEN` | Fly, git |
| Vercel org and project ids, Supabase URL, anon key, API URL | GitHub environment variables and Vercel environment variables | secrets stores need not hold them; they are public in the SPA bundle |
| `HARRIER_AUTH`, `HARRIER_SUPABASE_URL`, `HARRIER_ALLOWED_HOSTS`, `HARRIER_CORS_ORIGINS` | `[env]` in the app's Fly configuration file | none of them is secret |
| The service role key, any `sb_secret_` key | nowhere in this pipeline | Fly (spec 104 refuses to start), GitHub, Vercel, git |

The point of the split: GitHub never holds the API's password, Fly never
holds the migrator's, and neither holds the service role. Fly secrets are
app wide, reach every machine as environment variables, and cannot be read
back; that is why the migrator is not one of them (Options weighed).

Fly deploy tokens are created per app with `fly tokens create deploy -a
<app> -x <expiry>`, so the staging token cannot deploy production. GitHub
gives an environment's secrets only to jobs that name the environment,
and only after its required reviewer approves.

### one deploy

`deploy.yml`, job per environment, `concurrency` group per environment so
two deploys of one environment never interleave:

1. **Build once.** The `hosted` target is built from the commit and pushed
   to the app's Fly registry, tagged with the commit SHA. Production
   deploys the image staging ran, by digest, not a rebuild.
2. **Migrate.** `uv run harrier store migrate` with
   `HARRIER_DATABASE_URL` set from `HARRIER_MIGRATE_DATABASE_URL` in the
   step's environment. It prints `postgres N -> M`. A refusal exits 1 and
   the deploy stops here; the running API is untouched.
3. **Grant.** `uv run harrier store grant-tenant <login role>`, the role
   named by an environment variable. Idempotent.
4. **Check.** `uv run harrier store check-deploy --login <login role>`.
   Exit 1 stops the deploy.
5. **Deploy the API.** `fly deploy --image <digest> --config
   deploy/fly.<env>.toml`. Each new machine runs spec 104's start checks;
   a machine that refuses never passes its health check, and Fly does not
   route to it.
6. **Smoke.** `GET https://<api host>/health` returns 200 and its
   `revision` equals the commit SHA. A `GET` of `/rest/v1/jobs` on the
   Supabase project with the anon key is not a 2xx response.
7. **Deploy the SPA.** `vercel pull`, `vercel build --prod`, `vercel
   deploy --prebuilt --prod`. Last, so a new SPA never talks to an old API.

Staging runs on every push to `main` whose CI run passed. Production runs
on `workflow_dispatch` with a commit SHA input; the job refuses a SHA with
no successful staging deploy, and waits for Akin's approval in the
`production` environment. Self-review is allowed, because Akin is the only
reviewer.

**The rule a migration must keep.** Machines of the old release keep
serving through step 2 to step 5, against the new schema. Spec 104 checks
the version at start only, so they keep running. Every migration must
therefore leave the previous release working: it adds, and removes only
what the previous release no longer reads. A migration that cannot is
deployed with the API scaled to zero first, which the runbook describes
and which is downtime. Nothing checks this rule automatically (Honest
limitations).

### `harrier store grant-tenant`

`harrier store grant-tenant <role>`, run as the migrator on a Postgres
store at `target_version()`:

- refuses, exit 1, naming the check and never the URL, when the role does
  not exist, cannot log in, is not `NOINHERIT`, is a superuser, has
  `BYPASSRLS`, is a member of `service_role` or `postgres` directly or
  through another role, or owns a table in `public`. These are spec 104's
  start checks, called from the same function, so the deploy fails before
  a machine does;
- otherwise runs `GRANT harrier_tenant TO <role>`, the name quoted as an
  identifier, and prints `harrier_tenant granted to <role>`, or
  `harrier_tenant already granted to <role>` when it was;
- on a SQLite store, refuses: `error: harrier store grant-tenant needs a
  Postgres store`.

Creating the login role and its password stays a one-time operator step
per environment, in the runbook: `CREATE ROLE ... LOGIN NOINHERIT`, the
password set with `\password` so it is hashed before it leaves psql, then
`fly secrets set HARRIER_DATABASE_URL=...` from the same shell. A password
never passes through the workflow.

### `harrier store check-deploy`

Run as the migrator after the grant. Exit 0 prints one line per check
passed. Exit 1 names the first check that failed:

- the store is at `target_version()`;
- `anon`, `authenticated` and `service_role` hold no privilege on any
  table, sequence or function in `public` that harrier created (the
  grants spec 105's catalog test asserts, read from the live catalog);
- every table in `OWNED_TABLES` has row security enabled and forced;
- the login role passes the checks above and is a member of
  `harrier_tenant` (and of no role the deploy did not grant: "Amendment
  (2026-10-11): login roles reach no role past the policy").

It writes nothing. The REST probe in step 6 is a separate HTTP check
because the Data API setting lives outside the database.

### classification

Before any file below is added:

| Path | Class | Entry |
|---|---|---|
| `deploy/fly.staging.toml`, `deploy/fly.production.toml` | public | none needed; no never-in-git pattern matches, and they hold no secret |
| `apps/web/vercel.json` | public | none needed |
| `.github/workflows/deploy.yml` | public | none needed |
| `docs/deploy.md` | public | none needed |
| `.vercel/**`, `apps/web/.vercel/**` | never-in-git | `vercel link` and `vercel pull` write project ids and pulled environment values there |
| `.env.staging`, `.env.production` | never-in-git | named one by one: a `.env.*` glob would also match the tracked `.env.example`, and `services/api/tests/test_classification_coverage.py::test_never_in_git_paths_are_absent_from_index` matches with `fnmatch` |

`.gitignore` and `.dockerignore` gain `.vercel/`. The two env names are
already ignored by `.env.*` in both files. No `supabase/` directory is
added: ADR-013 decision 5 rules out Supabase CLI migrations, and project
settings are set in the dashboard and recorded in the runbook. A later
change that adds Supabase CLI files classifies them first.

### `.env` and hosted URLs

- `docker-compose.yml`'s `environment:` gains `HARRIER_DATABASE_URL: ""`
  and `HARRIER_AUTH: ""`. `environment` wins over `env_file`, which the
  file already relies on for `HARRIER_BACKUP_DIR` (`docker-compose.yml:72-76`),
  so whatever `.env` says, the local container runs on its SQLite file in
  local mode (ADR-013 decision 9).
- `store_target` refuses a `postgresql://` URL read from `.env`, not from
  the environment, whose host is not `localhost`, `127.0.0.1` or `::1`:
  `error: HARRIER_DATABASE_URL in .env names a remote store; a hosted
  store's URL is a credential, so export it in the shell for the command
  that needs it`. Exit 1. An exported URL, and a loopback URL in `.env`
  for a local test server, behave as today (open decision 5).
- `.env.example` says, above the variable, that a hosted URL never goes
  in `.env`.

### hosted logs

Hosted means `HARRIER_AUTH=supabase` (spec 104). Locally nothing changes.

- **Where.** Standard error only. No file handler and no `data/logs`.
  `configure_logging` opens no SQLite file: today it calls `connect()`
  (`logsetup.py:138`), which on a hosted machine would create and migrate
  an empty local tracker in the image's data directory.
- **What a record carries.** Redaction by literal value cannot work
  hosted. It needs every owner's identity values in one process, read past
  the row policy, and the refresh at the write path
  (`services/api/src/harrier/tracker/store.py:701-704`) would load one
  owner's values and drop every other's. Instead the hosted formatter
  renders each record's template with its arguments typed: an `int`,
  `float` or `bool` argument as written, every other argument as `[text]`.
  An exception renders as its class and its frames (file, line, function),
  without its message. The template is code in this public repository; an
  argument is data. A search of `services/api/src` for `.debug(`,
  `.info(`, `.warning(`, `.error(`, `.exception(` and `.critical(` on
  `logger`, `log`, `_log`, `LOGGER` or `root` finds 55 calls and none
  passes an f-string, so every value in them is an argument (counted with
  `grep -rEn` on 2026-10-10).
- **Access lines.** Uvicorn's access line is written with the path and
  without the query string.
- **Prompt logging.** The hosted API refuses to start when `AI_DEBUG`,
  `CODEX_CLI_DEBUG`, `CLAUDE_CLI_DEBUG`, `OPENAI_DEBUG` or
  `ANTHROPIC_DEBUG` is set truthy (`services/api/src/harrier/llm/config.py:161-169`).
  With any of them on, `_log_call` writes prompts and outputs under
  `data/llm-logs` (`services/api/src/harrier/llm/providers.py:46-55`),
  which hosted would be tenants' application content on a shared machine.
  Text: `error: prompt logging (<variable>) is local only; unset it for the
  hosted API`. Exit 1, naming the variable, not its value.
- **Retention.** Fly keeps searchable logs for 7 days and does not say
  where the store runs. Nothing here ships logs elsewhere.

### error reporters

None. `services/api/pyproject.toml` and `apps/web/package.json` name no
error reporting SDK; the only "Sentry" in `services/api/src` is a resume
vocabulary entry (`services/api/src/harrier/resume/vocabulary.py:139`). A
test fails if `sentry-sdk`, `@sentry/` packages, `rollbar`, `bugsnag` or
`honeybadger` appear in either manifest. Adding one is its own spec, which
must turn local variables off (Sentry's Python SDK captures them by
default, `include_local_variables` defaults to `True`) and apply the
hosted log rule to what it sends, because a frame of `postgres_connect`
holds the URL and its password.

## Failure modes

- **A migration fails in the deploy.** Step 2 exits 1, the store stays at
  its version, and no image is deployed. The API keeps serving.
- **The grant or a deploy check fails.** Steps 3 and 4 exit 1 naming the
  check. The store may already be at the new version; old machines keep
  serving it under the migration rule above.
- **`fly deploy` fails after a migration.** The store is ahead of the
  running release. Running machines keep serving; any of them that
  restarts is refused by spec 104 (store ahead) and stays down. The fix is
  to deploy forward, a fixed image, not to roll the schema back: harrier
  has no down migrations (spec 090). The runbook says so.
- **A new machine refuses to start.** Its health check never passes, Fly
  keeps the old machines, and the deploy reports failure.
- **The REST probe gets a 2xx.** The Data API is on and exposes something.
  The deploy fails at step 6, after the API is live but before the SPA.
  The runbook's fix is the dashboard setting.
- **Production asked for a commit staging never ran.** Refused before the
  environment's secrets are released.
- **Two deploys of one environment.** The second waits for the first
  (concurrency group); two migrations would be serialized anyway by spec
  103's advisory lock.
- **A pull request from a fork.** It runs CI, never `deploy.yml`, and
  never sees an environment secret.
- **A hosted URL in `.env`.** The CLI refuses it (open decision 5), and
  the local container never receives it.
- **Prompt logging turned on hosted.** The API refuses to start.
- **A log line whose argument is a name.** Rendered `[text]` hosted. A
  name typed into a log template itself, rather than passed as an
  argument, is printed; review is the only guard (Honest limitations).
- **The pooler drops `SET LOCAL`.** Spec 104's read-back answers 500.
  Spec 115's A5 finds this before any deploy depends on it.
- **A deploy token or the migrator URL leaks.** Revoke the token with `fly
  tokens revoke`, reset the role's password in the dashboard, and update
  the GitHub secret. The runbook lists each.

## Acceptance criteria

- [ ] `docker build --target hosted` produces an image that installs the
      `postgres` and `hosted` groups and `pdf`, holds no `claude` binary and
      no `apps/web/dist` (planned test_the_hosted_image_installs_the_store_and_auth_groups,
      planned test_the_hosted_image_carries_no_cli_provider_and_no_spa),
      and the local image's tests in `services/api/tests/test_container.py`
      pass unchanged, including
      `services/api/tests/test_container.py::test_the_image_installs_every_feature_dependency_group`.
- [ ] Both Fly files set `primary_region = "ams"`, `HARRIER_AUTH =
      "supabase"` in `[env]`, an HTTP health check on `/health` with a Host
      header from that file's `HARRIER_ALLOWED_HOSTS`, and no secret
      (planned test_the_fly_files_hold_no_secret_and_name_the_region).
- [ ] `apps/web/vercel.json` sets `git.deploymentEnabled` to `false`.
- [ ] `harrier store grant-tenant` refuses each case under Behavior, one
      test each, grants once, and reports an existing grant
      (planned test_grant_tenant_refuses_a_role_that_can_bypass,
      planned test_grant_tenant_grants_once).
- [ ] `harrier store check-deploy` fails, naming the check, for a store
      behind, a grant to `anon`, a table without forced row security, and
      a login role outside `harrier_tenant`, and passes on a migrated,
      granted store (planned test_check_deploy_names_the_failed_check).
- [ ] `deploy.yml` runs migrate, grant, check, API deploy, smoke and SPA
      deploy in that order; production takes a SHA, refuses one without a
      successful staging deploy, and names the `production` environment
      (planned test_the_deploy_workflow_migrates_before_it_deploys, which
      reads the workflow file; Open decision 7 says why a file read is the
      proof here).
- [ ] The staging environment is deployed once by the workflow: the pull
      request records the run, `GET /health` with the commit's revision,
      and the REST probe's non-2xx status.
- [ ] `config/data-classification.json` lists `.env.staging`,
      `.env.production`, `.vercel/**` and `apps/web/.vercel/**`;
      `.gitignore` and `.dockerignore` ignore `.vercel/`; and
      `services/api/tests/test_classification_coverage.py::test_never_in_git_paths_are_gitignored`
      and
      `services/api/tests/test_container.py::test_every_never_in_git_path_is_kept_out_of_the_image`
      pass with them.
- [ ] With `HARRIER_DATABASE_URL` and `HARRIER_AUTH` set in `.env`, the
      compose file still hands the container empty values
      (planned test_the_local_container_never_takes_a_store_from_dotenv).
- [ ] A remote Postgres URL in `.env` is refused with the text under
      Behavior; the same URL exported is accepted; a loopback URL in
      `.env` is accepted (planned test_a_remote_store_url_in_dotenv_is_refused).
- [ ] Hosted, `configure_logging` opens no SQLite file and adds no file
      handler (planned test_hosted_logging_opens_no_database_and_no_file).
- [ ] Hosted, a record with a text argument and one with an integer render
      as `[text]` and the integer, and an exception renders without its
      message (planned test_hosted_records_carry_no_text_arguments).
- [ ] Hosted, the access line carries no query string (planned
      test_hosted_access_lines_drop_the_query).
- [ ] The hosted API refuses to start with each prompt-logging variable
      set, naming it (planned test_hosted_start_refuses_prompt_logging).
- [ ] No error reporting SDK is a dependency (planned
      test_no_error_reporter_is_installed).
- [ ] `docs/deploy.md` covers the one-time setup of each environment, each
      Supabase setting with its check, every secret's home from the table,
      a failed deploy after a migration, and revoking each credential.
- [ ] `uv run ruff check`, `uv run pyright`, `pnpm type-check` and
      `pnpm lint` pass; `just check` passes.

## Honest limitations

- The migration rule (each migration leaves the previous release working)
  is a rule, not a check. A migration that breaks it takes the running
  release down until the new one is up.
- No down migrations, so a bad migration is fixed forward.
- GitHub-hosted runners are taken to reach the internet over IPv4 only, so
  the migrator connects through Supabase's session pooler, while
  Supabase's guide recommends a direct connection for migrations. Not
  confirmed from GitHub's documentation; the first staging deploy shows
  whether the pooler path works, and spec 115's A5 checks the pooler
  itself.
- The Vercel token is account wide (or team wide) and stronger than the
  job needs. Vercel offers no per-project deploy token in the pages read.
- Fly's log store keeps 7 days and does not say where it runs. Hosted logs
  carry no text arguments, which limits what that store can hold, not
  where it is.
- Typed rendering hides every text argument, including harmless ones such
  as a status name. Debugging hosted needs the code's templates and ids.
- A value written into a log template rather than passed as an argument
  is printed. No test can find that in general.
- Backups of the hosted store are Supabase's, per its plan. This spec adds
  none; a dump of tenants' data is personal data on another machine and
  needs its own spec.
- Cost figures appear only under "Hosting budget (2026-10-11)", each quoted
  from a cited page with its read date or marked as an estimate with its
  arithmetic. Billing tenants stays out of scope (spec 102).
- The line numbers cited here are refreshed by the change that implements
  this spec, not before (decision D9, 2026-10-11).

## Migration

None for the local product. The two compose environment lines change
nothing for an operator whose `.env` names no store URL, which is every
local install today, since every command but `harrier store` refuses one
(spec 103). An operator with a remote URL in `.env` is told to export it
instead.

Hosted, the first deploy of each environment follows the runbook's
one-time setup: project, settings, the login role and its Fly secret, the
GitHub environment and its secrets, then the workflow.

## Changes to approved specs

Each line below is a change this spec asks of an approved spec. None is
written into that spec here; each is an amendment Akin approves with this
spec.

- **Spec 103:** `store_target` refuses a remote `postgresql://` URL read
  from `.env` (Behavior, "`.env` and hosted URLs"), where its Corrections
  accept one; and `harrier store` takes two subcommands, `grant-tenant`
  and `check-deploy` (Scope items 4 and 5).
- **Spec 104:** hosted start-up also refuses prompt logging (Behavior,
  "hosted logs"). Its Configuration sentence "The API reads no `.env`
  file itself" is corrected as Out of scope records.

## Options weighed

- **Migrate in Fly's `release_command`.** It runs in a temporary machine
  from the new image and stops the deploy on a non-zero exit, which is the
  ordering wanted. But it reads the app's secrets, and Fly secrets reach
  every machine of the app, so the migrator's URL would sit in the API's
  environment, where run subprocesses inherit it (spec 104 refuses the
  service role key there for the same reason). Not chosen: the workflow
  migrates, from GitHub, and Fly never holds the migrator.
- **Migrate on API start.** Rejected by spec 103: requests run as a role
  that cannot run DDL, and a deploy should migrate once.
- **Supabase CLI migrations.** Rejected by ADR-013 decision 5.
- **Vercel's Git integration deploying the SPA.** Simpler, but it deploys
  on push regardless of whether the API deployed, so a new SPA can meet an
  old API. Not chosen.
- **A Vercel custom environment `staging` in one project.** Pro plan only,
  one per project. Two projects give the same separation on any plan and
  match the per-environment Supabase projects and Fly apps (open decision
  4).
- **A Management API check of the Data API setting.** Exact, but needs a
  Supabase personal access token in GitHub, which manages every project on
  the account. Not chosen: the REST probe uses the public anon key and
  tests the property itself.
- **Per-owner redaction hosted.** Load the requesting owner's identity
  values in each request's transaction and redact those. Keeps text in
  logs, costs queries per request, and a log line outside a request is
  unredacted. Open decision 6's alternative.
- **A separate hosted Dockerfile.** Clearer, but duplicates the Chromium,
  poppler and dependency layers the local image already proves. Open
  decision 3's alternative.

## Decisions taken (2026-10-11)

Akin took these in chat on 2026-10-11 ("go with your recommendations").

- **D4, this spec takes what specs 106 to 111 ask of it.** Specs 106, 107,
  108, 109 and 111 each list an ask of this spec, under "Changes to
  approved specs" or as assumptions for its staging check. They are
  gathered into one section, "Amendment (2026-10-11): what specs 106 to
  111 ask of the deploy", with its own acceptance criteria. Gathering them
  found one question none of them settles: where the scheduler runs on
  Fly, now Open decision 9 (superseded the same day by the hosting
  budget, which asks Open decision 10 instead).
- **D9, line numbers.** Refreshed by this spec's implementing change
  (Honest limitations).

Open decisions 1, 2 and 4 to 8 below were not the subject of these
decisions and stand as written; item 3 is answered for `gmail` only.
Item 2 is superseded by "Region (2026-10-11)", a later decision of the
same day.
Item 9 is superseded by the hosting budget of the same day, and item 10
is added by it (Hosting budget (2026-10-11)).

## Open decisions for Akin

1. **The first slice as its own spec.** Applied in this text: the slice
   is spec 115, "Spec 105 holds on a real Supabase project", approved and
   built before spec 112's second pull request; this spec depends on 115
   and is refined again once A1 to A10 are known, because 115's A5 and A10
   change this design if refuted. Spec 112's `depends` gaining 115 is an
   amendment listed in spec 115. Alternative: the slice as phase 1 of this
   spec, which means approving the pipeline before its assumptions are
   tested.
2. **Regions.** Recommended: Frankfurt for both, Fly `fra` and Supabase
   `eu-central-1`. Alternative: Paris (`cdg`, `eu-west-3`) or Stockholm
   (`arn`, `eu-north-1`), equally EU.
   Superseded 2026-10-11: Supabase `eu-west-1` (Ireland), Fly `ams`
   (Amsterdam). Region (2026-10-11) gives the reasons.
3. **The hosted image.** Recommended: a `hosted` target in `Dockerfile`,
   sharing the base, Chromium and poppler layers, with `postgres`, `hosted`
   and `pdf`, without the `claude` CLI (ADR-013 decision 8) and without the
   SPA (decision 7); `gmail` waits for spec 109. Alternative: a separate
   `deploy/hosted.Dockerfile`.
   On `gmail` only, answered 2026-10-11 (D4): spec 109 offers no hosted
   Gmail, so the hosted image installs no `gmail` group. The rest of this
   item stands as written.
4. **Vercel environments.** Recommended: two Vercel projects, each
   deploying to its own Production environment. Alternative: one project
   with a `staging` custom environment, which needs the Pro plan.
5. **A remote URL in `.env`.** Recommended: refused by `store_target`, so
   the rule "export it in the shell" is enforced. Alternative: the compose
   override and documentation only, leaving the CLI to read it.
6. **Hosted log content.** Recommended: typed rendering, no text argument
   and no exception message leaves the process. Alternative: per-owner
   redaction loaded in each request (Options weighed).
7. **How the workflow's order is proven.** Recommended: a test that reads
   `deploy.yml` and asserts the step order, plus the recorded staging run.
   A file read is a last resort under `review-response`; here the property
   is the file's own order, and no runner can execute GitHub's workflow
   locally. Alternative: the recorded run only.
8. **The API's connection path.** Recommended: Supabase's session pooler
   for both the API and migrations: one path, IPv4, and session state
   kept, which spec 112's session-level migration lock needs. Alternative:
   a direct IPv6 connection from Fly for the API, which Supabase
   recommends for persistent servers and which one Fly community report
   found failing from one region.
9. **Where the scheduler runs on Fly.** Added 2026-10-11 by the Amendment
   below. Recommended: an app of its own per environment, deployed from
   the same image, so its secrets (its login's URL, and what spec 106's
   run start needs) are its own and the API's never reach it, which keeps
   spec 108's rule that the scheduler never runs as the API's login.
   Alternative: a process group of the API app, as spec 108 wrote it, one
   app fewer, with every secret of each reaching the other's machines
   (Fly secrets are app wide).
   Superseded 2026-10-11 by the hosting budget: an always-on scheduler
   machine costs more than the budget allows (Hosting budget, "The
   scheduler"). The question of which app holds the scheduler's secrets
   survives as Open decision 10.
10. **Which app holds the scheduled scheduler machine.** Added 2026-10-11
   Taken 2026-10-11 (Akin, "go with your recommendations"): the recommended answer.
    by the hosting budget. Recommended: an app of its own per environment,
    `harrier-scheduler-staging` and `harrier-scheduler-production`, with
    no service and no IP address, holding one scheduled machine from the
    API's image. Its secrets stay its own, for the reason Open decision 9
    gave, and it needs its own deploy token, since a Fly deploy token
    manages one app. Alternative: the scheduled machine inside the API
    app, one app and one token fewer, with the API's secrets reaching the
    scheduler and the scheduler's reaching the API machine. Fly's pricing
    page bills machines, stopped machines' root file systems, volumes,
    IPv4 addresses and data transfer; whether an app with none of those
    costs anything is not stated there and is an assumption (spec 115,
    F4).

## Proof / origin

- `docs/adr/ADR-013-hosted-multi-tenant-deployment.md`, decisions 3, 5, 7,
  8 and 9, decision 1's Amendment (spec 105) and Consequences (backups).
- `specs/102-hosted-multi-tenant-deployment.md`, open decisions 2, 4 and 5.
- `specs/103-the-tracker-store-speaks-postgres.md`: Honest limitations
  (traceback locals, Postgres major version) and Corrections (the API reads
  `.env`).
- `specs/104-the-hosted-api-authenticates-every-request.md`: The store
  version, The login role, Configuration, Migration, and its assumptions.
- `specs/105-every-personal-table-is-owned-and-policed.md`: open decision 6,
  Honest limitations, Amendment.
- `specs/112-the-tracker-domain-runs-on-either-store.md`: Out of scope
  (pooling, image) and the session-level migration lock.
- `specs/115-spec-105-holds-on-a-real-supabase-project.md`: the first
  slice, split from this spec, and its assumptions A1 to A10.
- Code read on `main` at 44ac0d7: `services/api/src/harrier/pgstore.py`,
  `services/api/src/harrier/tracker/schema.py:690-923`,
  `services/api/tests/pg_support.py`, `services/api/tests/test_owner_policy.py`,
  `services/api/src/harrier/logsetup.py`, `services/api/src/harrier/logredact.py`,
  `services/api/src/harrier/llm/config.py`, `services/api/src/harrier/llm/providers.py`,
  `docker-compose.yml`, `Dockerfile`, `.dockerignore`, `.gitignore`,
  `config/data-classification.json`,
  `services/api/tests/test_classification_coverage.py`,
  `services/api/tests/test_container.py:247`, `.github/workflows/ci.yml`.
- Supabase, Restricting access on auth, storage and realtime schemas
  (2025-04-21): functions may not be created in `auth`; triggers and RLS
  policies on `auth.users` and foreign keys to it stay allowed. Read
  2026-10-10.
  https://supabase.com/changelog/34270-restricting-access-on-auth-storage-and-realtime-schemas-on-april-21-2025
- Supabase, Security patch notice: entities in `auth` are owned by
  `supabase_admin` and "explicitly granted all permissions to `postgres`";
  `postgres` gains some superuser-only abilities, so it is not a
  superuser. Grant option not stated (spec 115's A2). Read 2026-10-10.
  https://supabase.com/changelog/9314-security-patch-notice
- Supabase, Row Level Security: "On Supabase the owner is `postgres`,
  which has `bypassrls`". Read 2026-10-10.
  https://supabase.com/docs/guides/database/postgres/row-level-security
- Supabase, Postgres roles: `postgres` "has admin privileges"; a separate
  login role per service. Attributes not listed (spec 115's A4). Read
  2026-10-10.
  https://supabase.com/docs/guides/database/postgres/roles
- Supabase, Connecting to Postgres: direct connections are IPv6 unless the
  IPv4 add-on; the shared pooler is IPv4, session mode on 5432 keeps
  session state, transaction mode on 6543 has no prepared statements;
  custom roles connect to the pooler as `[ROLE].[PROJECT-REF]`; migrations
  should use a direct connection. Read 2026-10-10.
  https://supabase.com/docs/guides/database/connecting-to-postgres
- Supabase, Securing your API: "Enable Data API" off, and then "none of
  the auto-generated REST endpoints respond, regardless of grants or RLS".
  Read 2026-10-10. https://supabase.com/docs/guides/api/securing-your-api
- Supabase, Regions: the six European regions, and the generic "Europe"
  choice possibly landing in London or Zurich. Read 2026-10-10.
  https://supabase.com/docs/guides/platform/regions
- Fly, Regions: `ams`, `arn`, `cdg`, `fra`, `lhr` in Europe. Read
  2026-10-10. https://docs.fly.io/reference/regions
- Fly, App configuration: `release_command` runs in a temporary machine
  with the app's secrets and a non-zero exit stops the deploy; HTTP checks
  take `headers` (whether `Host` may be set is not stated, spec 104's
  assumption, checked by the first staging deploy); `primary_region`;
  `[env]` for non-sensitive values. Read 2026-10-10.
  https://docs.fly.io/reference/configuration
- Fly, Secrets: environment variables on every machine of one app, set
  with a restart, never readable back. Read 2026-10-10.
  https://docs.fly.io/apps/secrets
- Fly, Deploy tokens: `fly tokens create deploy` manages one app only.
  Read 2026-10-10. https://fly.io/docs/flyctl/tokens-create-deploy
- Fly, Search logs: "We currently retain logs for 7 days." Read
  2026-10-10. https://docs.fly.io/monitoring/search-logs
- Fly community, IPv6 outbound from one region to Supabase failing (one
  user's report, unconfirmed by Fly). Read 2026-10-10.
  https://community.fly.io/t/ipv6-outbound-from-gru-to-supabase-100-packet-loss/27739
- Fly, Pricing: new organizations are Pay As You Go with no free tier or
  monthly free allowance; shared-cpu-1x always on per month in the base
  region, 256MB $2.19, 512MB $3.69, 1GB $6.70; a region's price is the
  base-region price times a region multiplier listed on the page,
  Amsterdam's 1.038461538, so in Amsterdam 1GB is $6.95 and 512MB $3.84
  as the page's region selector shows them (Region (2026-10-11) gives the
  arithmetic); "Monthly Machine prices assume 30 days of
  continuous runtime"; a stopped Machine is billed only for its root file
  system, at $0.15/GB/month; volumes $0.15/GB/month; outbound data to
  Europe $0.02/GB; shared IPv4 and IPv6 free, dedicated IPv4 $2/month; no
  small-invoice waiver is stated. Read 2026-10-11.
  https://docs.fly.io/about/pricing
- Fly, Autostop/autostart: the proxy "stops Machines when the app has
  excess capacity", judged by each Machine's concurrency `soft_limit`;
  its stop loop "runs every few minutes and stops at most one Machine per
  region per pass"; `min_machines_running` applies only in the primary
  region and only when stopping is on; an app may instead "exit when
  idle" with `auto_stop_machines = "off"` and `auto_start_machines =
  true`. The page does not say whether an open connection keeps a Machine
  running, and does not mention background work. Read 2026-10-11.
  https://docs.fly.io/launch/autostop-autostart
- Fly, `fly machine run`: `--schedule` starts a Machine on a fuzzy
  `hourly`, `daily`, `weekly` or `monthly` cycle, "once per (approximate)"
  interval; scheduled Machines "cannot be started via flyctl or Machines
  API commands"; they suit Machines "that do a finite job, then exit";
  the default restart policy is `always` unless `--rm`, which destroys the
  Machine when it stops. Read 2026-10-11.
  https://docs.fly.io/machines/flyctl/fly-machine-run
- Fly, Task scheduling: scheduled Machines are "Not for precise timing".
  Read 2026-10-11. https://docs.fly.io/blueprints/task-scheduling
- Vercel, Hobby plan: free; restricted to non-commercial, personal use
  under the fair use guidelines; function maximum duration 300 s; when a
  usage limit is exceeded the feature pauses until 30 days pass. Read
  2026-10-11. https://vercel.com/docs/plans/hobby
- Vercel, Environments: custom environments on Pro (one per project) and
  Enterprise; staging alternatives. Read 2026-10-10.
  https://vercel.com/docs/deployments/environments
- Vercel, Git configuration: `git.deploymentEnabled: false` turns off
  automatic deployments for every branch. Read 2026-10-10.
  https://vercel.com/docs/project-configuration/git-configuration
- Vercel, GitHub Actions guide: `vercel pull`, `vercel build --prod`,
  `vercel deploy --prebuilt --prod`; org and project ids from
  `.vercel/project.json`. Read 2026-10-10.
  https://vercel.com/kb/guide/how-can-i-use-github-actions-with-vercel
- GitHub, Deployments and environments: environment secrets reach only
  jobs naming the environment, after approval; required reviewers on
  public repositories on every plan; self-review can be prevented. Read
  2026-10-10.
  https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments
- Sentry, Python options: `include_local_variables` defaults to `True`.
  Read 2026-10-10. https://docs.sentry.io/platforms/python/configuration/options/

Assumptions this spec could not confirm, each checked where named:

- Supabase's pooler keeps `SET LOCAL` through transaction mode (spec
  115's A5).
- GitHub-hosted runners have no IPv6 route (Honest limitations); first
  staging deploy.
- Fly's health check may set `Host` (spec 104's assumption); first staging
  deploy.
- The Data API toggle leaves Auth and Storage working; checked when spec
  114's sign-in first runs against staging.
- The REST endpoint answers non-2xx with the Data API off; the first
  staging deploy's smoke step.
- The hosting budget's Fly assumptions (Hosting budget (2026-10-11),
  "Assumptions"): spec 115's F1 to F5.

## Out of scope

Where an item below gives a deploy step to specs 106 to 111, the
Amendment (2026-10-11) at the end of this spec takes that step here.

- Billing tenants, and any paid plan. The operator's hosting budget and
  the plans it chooses are under "Hosting budget (2026-10-11)".
- Running spec 105's isolation tests on a real Supabase project, and CI
  migrating as a non-superuser: spec 115.
- Creating any account or project. Akin does that; the runbook lists it.
- Tenant credentials, run isolation and a run's environment: spec 106.
- Storage buckets and their policies: spec 107.
- The hosted scheduler and its machines: spec 108.
- Gmail and Telegram hosted, and the `gmail` group in the hosted image:
  spec 109.
- Sign-up, invitations, export and deletion: spec 111. This spec only
  records that public sign-up is off at project creation.
- The SPA's sign-in and its environment variable names: spec 114. This
  spec puts the values where Vercel builds them.
- Backups beyond Supabase's own, custom domains, and preview deployments
  (spec 104, open decision 7: no preview origins).
- An error reporter, beyond the rule and the test that none exists.
- Found while drafting, each for its own spec or amendment:
  - Spec 104's Configuration says "The API reads no `.env` file itself
    (`grep -rn dotenv services/api/src` finds nothing)". Since spec 103's
    Corrections, `store_target` reads `HARRIER_DATABASE_URL` from `.env`
    when it is not exported (`connection.py:228-234`, in `pgstore.py`
    until spec 112's PR 1 moved it on 2026-10-11; through
    `harrier.envfile`, which the grep does not match). `HARRIER_AUTH` is
    still read from the environment only, so the conclusion about hosted
    mode holds; the sentence does not.
  - This spec's stub cited `schema.py:853` for the latest migration, said
    the store was at version 10, and placed `postgres_connect` at
    `pgstore.py:137-149`. All three were stale. `postgres_connect` is at
    `pgstore.py:64-81`, and the latest version is whatever
    `target_version()` (`pgstore.py:84-88`) returns. (These two were
    stale as well, at `:173-190` and `:193-197`; corrected 2026-10-11.) This text and its
    acceptance criteria cite `target_version()` rather than a number, so
    they do not go stale again.

## Amendment (2026-10-11): what specs 106 to 111 ask of the deploy

Akin took decision D4 on 2026-10-11: this spec takes what specs 106, 107,
108, 109 and 111 ask of it, each listed there under "Changes to approved
specs" or as assumptions for this spec's staging check. They are gathered
here as one amendment. Where Out of scope above gives one of these steps
to specs 106 to 111, read this section. Each item names the spec and
section that asked it.

### The API app

- **One machine, stopped when idle** (spec 106, "Where a run lives on
  Fly"; amended 2026-10-11 by the hosting budget, which replaced "never
  stopped"). Both Fly files set the API service's auto-stop settings as
  "Hosting budget (2026-10-11)" states. A step after `fly deploy` counts
  the API process group's machines, started or stopped, and fails the
  deploy unless there is exactly one.
- **The run login role** (spec 106, "One login role for requests, another
  for runs"). The operator creates `harrier_run` once per environment, as
  `harrier_api` is created (`CREATE ROLE ... LOGIN NOINHERIT`, the
  password set with `\password`), and sets `HARRIER_RUN_DATABASE_URL` as a
  Fly secret on the API app from the same shell. Deploy step 3 runs
  `harrier store grant-tenant` for `harrier_run` as for `harrier_api`, and
  `check-deploy` applies its login role checks to both.
- **The credentials key** (spec 106, Migration; spec 116, Migration).
  `HARRIER_CREDENTIALS_KEY` is a Fly secret on the API app, set once per
  environment by the operator.
- **The publishable key** (spec 107, "Configuration").
  `HARRIER_SUPABASE_PUBLISHABLE_KEY` is not a secret and sits in `[env]`
  in both Fly files, beside `HARRIER_SUPABASE_URL`.
- **No operator notification settings** (spec 109, Migration). Neither
  Fly file names a `TELEGRAM_` or `GMAIL_` variable, and the runbook sets
  none as a Fly secret; spec 109's start check refuses the API otherwise.
- **No `gmail` group in the hosted image** (spec 109, Scope item 4). Open
  decision 3 left the group to spec 109, which offers no hosted Gmail.

### The scheduler

From spec 108, "What spec 110 provisions", as amended 2026-10-11 by the
hosting budget:

- **One scheduled machine**, created once per environment with `fly
  machine run --schedule hourly --restart no` from the API's image,
  running `harrier operator scheduler`, which runs one tick and exits
  (spec 108). It has no service. The process group with an always-on
  machine that this item first named is superseded by the hosting budget.
  Which app holds it is Open decision 10. Each deploy, after the API's
  `fly deploy`, moves it to the same image digest with `fly machine
  update`, which must keep its schedule (spec 115, F3).
- **The scheduler's login role.** The operator creates `harrier_scheduler`
  once per environment, as above. Two named deploy steps grant it
  `harrier_tenant` (`harrier store grant-tenant`) and
  `harrier_schedule_reader`, each refusing a role that fails spec 104's
  login role checks and each idempotent. Its database URL is a direct or
  session-mode connection, set as a secret where the scheduler runs.
  `check-deploy` fails when `harrier_scheduler` is not a member of both
  roles.
- **Its limits.** `HARRIER_SCHEDULER_CONCURRENCY` and
  `HARRIER_SCHEDULED_RUN_TIMEOUT_SECONDS` are set on the scheduler machine
  for each environment, as its environment variables (`fly machine run
  --env`). The run timeout also bounds what one tick can cost (Hosting
  budget). Their values are the operator's
  choice per environment; this amendment sets none.

### Storage

From spec 107, "Changes to approved specs" and Failure modes:

- **Other policies on `storage.objects`.** `check-deploy` fails, naming the
  policy, when `storage.objects` carries a policy other than
  `harrier_files_owner_only`.
- **The bucket's backup.** `docs/deploy.md` states what backs up
  `harrier-files`: until a spec adds a backup, nothing does, and a deleted
  or overwritten object is gone (spec 107, Honest limitations).
- **Hosted logging** of the LLM debug log and the log file is already this
  spec's ("hosted logs" under Behavior).

### Auth settings

From spec 111, "Changes to approved specs":

- **Checked at every deploy.** The smoke step (step 6) also reads the
  project's public Auth settings, `GET /auth/v1/settings` with the
  publishable key, and fails the deploy when `disable_signup` is false,
  when `external.anonymous_users` is true, or when any OAuth provider in
  `external` is true (every entry but `email`, `phone` and
  `anonymous_users`). The auth server's `Settings` handler returns these
  fields (`internal/api/settings.go`, read 2026-10-11).
- **The operator login.** The runbook creates, once per environment, the
  login that can `SET ROLE service_role` which `harrier operator erase`
  uses. Its URL and `HARRIER_SUPABASE_SECRET_KEY` live in the operator's
  shell for the command that needs them: never on Fly, in GitHub, in
  Vercel, in `.env` or in git.

### Where each secret lives, added

| Value | Lives in | Never in |
|---|---|---|
| `HARRIER_RUN_DATABASE_URL`, the `harrier_run` login | Fly secret on the API app | GitHub, Vercel, `.env`, git |
| `HARRIER_CREDENTIALS_KEY` | Fly secret on the API app | GitHub, Vercel, `.env`, git |
| The `harrier_scheduler` login's URL | a Fly secret of the app that holds the scheduler machine (Open decision 10) | GitHub, Vercel, `.env`, git |
| The operator login's URL and `HARRIER_SUPABASE_SECRET_KEY` | the operator's shell | Fly, GitHub, Vercel, `.env`, git |
| `HARRIER_SUPABASE_PUBLISHABLE_KEY` | `[env]` in the Fly files | not secret |

### Staging checks

Each is checked on the staging environment by the first deploy that needs
it, and the pull request records it as confirmed or refuted. A refuted
check stops the dependent spec until a spec owns the fix.

- **Spec 106** (Honest limitations): whether a process of the same user
  can read `/proc/<pid>/environ` of the API and of sibling runs in Fly's
  machine image; that Supabase's pooler, if the run URL goes through it,
  keeps `SET LOCAL` within one transaction; and that a rolling deploy
  replaces the one API machine, ending runs in flight. Its assumption that
  `auto_stop_machines = "off"` leaves the machine running is superseded by
  the hosting budget; what replaces it is spec 115's F1 and F2.
- **Spec 107** (Honest limitations): its assumptions 1 to 9 about the
  Storage server, and the Storage REST paths the seam calls.
- **Spec 108** (Honest limitations): that the migrating role owns
  `harrier_schedule_inputs()` and has `BYPASSRLS`; that it may create
  `harrier_schedule_reader` and grant it; and that the image's C library
  honors `TZ` for every zone `zoneinfo` accepts. Its assumption that a
  machine in a process group with no service is never stopped by Fly's
  proxy is superseded by the hosting budget, since no such group exists;
  what replaces it is spec 115's F3.
- **Spec 109** (Proof / origin, assumptions): that a bot cannot send to a
  private chat whose user has not written to it, and `sendMessage` then
  answers `ok: false`; that a Fly machine reaches `api.telegram.org` with
  no extra egress setting; and that the token pattern matches the tokens
  @BotFather issues.
- **Spec 111** (Proof / origin, assumptions for this spec's staging
  check, as its decision D6 left them).

### Honest limitations (amendment)

- **Fly secrets are app wide** (Where each secret lives). If the scheduler
  machine sits in the API app, the API's `HARRIER_DATABASE_URL` reaches
  the scheduler machine and the scheduler's login reaches the API
  machine, against spec 108's rule that the scheduler never runs as the
  API's login. Open decision 10 (which replaced Open decision 9 on
  2026-10-11).
- **The scheduler starts runs** through spec 106's run start (spec 108,
  "What this spec needs from spec 106's run binding"), which decrypts the
  owner's credentials. Wherever the scheduler runs, it then holds what a
  run start needs, `HARRIER_CREDENTIALS_KEY` and `HARRIER_RUN_DATABASE_URL`
  included. Spec 106's trust model names only the API process as holding
  the key; that table changes with spec 106, not here.
- **No value is chosen for the scheduler's limits.** The operator sets
  them per environment.

### Acceptance criteria (amendment of 2026-10-11)

- [ ] Both Fly files set `HARRIER_SUPABASE_PUBLISHABLE_KEY` in `[env]`
      and name no `TELEGRAM_` or `GMAIL_` variable; their auto-stop
      settings are the hosting budget's criterion below, which replaced
      `auto_stop_machines = "off"` on 2026-10-11 (planned
      test_the_fly_files_run_one_api_machine_that_stops_when_idle).
- [ ] The runbook's scheduler machine runs `harrier operator scheduler`
      on `--schedule hourly` with restart policy `no`, no service, and
      `HARRIER_SCHEDULER_CONCURRENCY` and
      `HARRIER_SCHEDULED_RUN_TIMEOUT_SECONDS` set; the always-on process
      group this criterion first named is superseded by the hosting
      budget. The deploy moves the machine to the API's image digest with
      `fly machine update` (planned
      test_the_deploy_workflow_migrates_before_it_deploys, extended).
- [ ] `deploy.yml` grants `harrier_tenant` to `harrier_api`, `harrier_run`
      and `harrier_scheduler`, and `harrier_schedule_reader` to
      `harrier_scheduler`, before `check-deploy`; counts the API machines,
      started or stopped, after `fly deploy`; and reads the Auth settings
      in the smoke step
      (planned test_the_deploy_workflow_migrates_before_it_deploys,
      extended).
- [ ] `check-deploy` fails, naming the check, when `harrier_run` or
      `harrier_scheduler` fails a login role check or lacks
      `harrier_tenant`, when `harrier_scheduler` lacks
      `harrier_schedule_reader`, and when `storage.objects` carries a
      policy other than `harrier_files_owner_only` (planned
      test_check_deploy_names_the_failed_check, extended).
- [ ] The Auth settings check fails on `disable_signup` false, on
      `external.anonymous_users` true and on an OAuth provider turned on,
      and passes on invite-only settings, against a fake settings response
      (planned test_the_deploy_refuses_open_sign_up).
- [ ] The hosted image installs no `gmail` group (planned
      test_the_hosted_image_installs_no_gmail_group).
- [ ] `docs/deploy.md` covers creating `harrier_run`, `harrier_scheduler`
      and the operator login, every secret in the table above with its
      home, and the statement on what backs up `harrier-files`.
- [ ] The first staging deploy that needs each staging check records it as
      confirmed or refuted in its pull request, and no refuted check is
      left without a spec that owns its fix.

## Hosting budget (2026-10-11)

Akin decided in chat on 2026-10-11: ADR-013's topology stays as written
(decision 7: Vercel serves only the SPA; the API and its runs stay on
Fly). Spec 121, which moved the API to Vercel, was withdrawn and its pull
request #228 closed. The hosted deployment targets under 5 USD per month.
Vercel stays on the free Hobby plan.

This section states what that changes in this spec. Where the text above
disagrees, this section holds, and each place it replaced says so. Specs
106 and 108 carry their own "Hosting budget (2026-10-11)" sections, and
spec 115 checks the assumptions below.

Every price below is quoted from Fly's pricing page or Vercel's Hobby
page, both read 2026-10-11 (Proof / origin). An Amsterdam machine price
is the page's base-region price times its Amsterdam multiplier, with the
arithmetic shown. Every other figure is marked as an estimate and shows
its arithmetic. The Amsterdam figures replaced Frankfurt ones on
2026-10-11 (Region (2026-10-11)).

### What the published prices allow

- Fly has no free tier and no monthly free allowance for a new
  organization. Every machine second counts.
- One shared-cpu-1x 1GB machine always on in Amsterdam costs $6.95 a
  month, for 30 days of continuous runtime: the base-region $6.70 times
  Amsterdam's multiplier, 6.70 × 1.038461538 = 6.96, which the page's
  region selector shows as $6.95. That alone is over the budget by 1.95
  USD (6.95 minus 5).
- **Estimate:** at that rate one machine hour costs about 0.0097 USD
  (6.95 / 720 hours). 5 USD buys 5 / 6.95 = 0.719 of a month, about 518
  machine hours of 720 (0.719 × 720 = 517.7). That is the ceiling for
  every running 1GB machine in Amsterdam together: the production API,
  the scheduler's ticks and the runs they start, and staging's machines
  while they run. The fixed costs below come off it first.
- A stopped machine is billed only for its root file system, at $0.15/GB
  a month. The hosted image's size is not measured. **Estimate:** with
  four machines (the API and the scheduler, in staging and production),
  each GB of root file system costs 0.60 USD a month while all four are
  stopped (4 × 0.15).
- Outbound data to Europe is $0.02/GB. The volume is not measured.
- A shared IPv4 address is free. A dedicated one is $2 a month, 40% of
  the budget (2 / 5). Neither app gets one.
- A volume is $0.15/GB a month and ties state to one machine. Hosted
  state lives in Supabase, so neither app has a volume.
- Vercel's Hobby plan is free.
- Supabase's Free plan is $0 a month and allows 2 active projects. The
  account already holds one other project, so only staging, the existing
  Supabase project named `harrier`, is on Free today; production is
  created when a Free slot is free or the plan changes (Region
  (2026-10-11)). It includes a 500 MB database, 1 GB of file
  storage, 5 GB of egress and 50,000 monthly active Auth users. Free
  projects pause after 1 week of inactivity, and the plan has no automatic
  backups (https://supabase.com/pricing, read 2026-10-11). Supabase Pro is
  from 25 USD a month, which alone exceeds the budget, so staging stays on
  Free and production is planned on Free. Creating production through a
  plan change instead would put the hosted deployment over the 5 USD
  target (25 is more than 5).

### Production on Fly

- One app, `harrier-api-production`, `primary_region = "ams"`, with one
  API machine: shared-cpu-1x, 1GB. The API image carries Chromium and
  poppler for PDFs (ADR-013 decision 7), which is why 1GB is chosen.
  512MB is an untested alternative. Its price in Amsterdam, from the
  page's multiplier: 3.69 × 1.038461538 = 3.83 USD a month always on,
  which the page's region selector shows as $3.84. That would fit an
  always-on machine under 5 USD with about 1.16 USD left for everything
  else (5 minus 3.84). Nothing shows 512MB holds Chromium during a run.
- The API's HTTP service sets `auto_stop_machines = "stop"`,
  `auto_start_machines = true` and `min_machines_running = 0`. Fly's
  proxy stops the machine when the app has excess capacity, judged by
  the concurrency `soft_limit`, in a loop that "runs every few minutes",
  and starts it on the next request. This replaces every
  `auto_stop_machines = "off"` requirement in this spec, its Amendment
  and its criteria.
- While any run is active, the API keeps its machine awake, and a run
  cut off by a stop is recorded as interrupted, never lost. Both are spec
  106's (its "Hosting budget (2026-10-11)").
- A shared IPv4 address and no dedicated one. No `[mounts]` and no
  volume.
- Still exactly one API machine, started or stopped (the Amendment's
  machine count check). No fly-replay (spec 106).

### Staging

- `harrier-api-staging` has the same settings as production. Its machine
  runs only while a request reaches it: the deploy's smoke step, a check
  of spec 115, or the operator. It then stops when idle, so between
  checks it costs its root file system only.
- Its scheduler machine is created without `--schedule` and started by
  hand (`fly machine start`) only for a check, so it too costs its root
  file system only between checks. Staging therefore does not exercise
  the hourly schedule; spec 115's F3 probes it.

### Vercel

- Both projects, `harrier-web-staging` and `harrier-web-production`, are
  on the Hobby plan and serve the SPA only. They run no function. Hobby's
  function limit of 300 s is one reason the API does not move to Vercel.
- **Constraint.** Hobby is restricted to non-commercial, personal use
  under Vercel's fair use guidelines. Invite-only personal use (spec 102,
  open decision 5) fits that. Charging tenants would require Vercel Pro
  and is out of scope.
- When a Hobby usage limit is exceeded, the feature pauses until 30 days
  pass. For the SPA that means the site can be down for up to 30 days.
  The limits themselves are not quoted here.

### The scheduler

An always-on scheduler machine of the same size would cost $6.95 a month
alone, over the budget before the API runs at all. Open decision 9 (an
always-on app of its own) and the Amendment's process group are
superseded. Spec 108's "Hosting budget (2026-10-11)" replaces them with a
Fly scheduled machine that runs one tick per start and exits, started on
Fly's `hourly` cycle. It is billed while it runs and costs its root file
system while stopped.

**Estimate:** an hourly cycle starts it at most 720 times in a 30-day
month (24 × 30). Its cost is the sum of those ticks' running time, each
the listing plus the runs it started, at the machine rate. A tick with
nothing due has not been timed. Spec 115's F4 projects it.

### The monthly cost check

Once a month the operator reads the Fly organization's billed usage for
the month so far, and Vercel's usage for both projects, and projects the
month as: spent so far / days elapsed × days in the month. The check
reports when the projection exceeds 5 USD, naming which line (machine
time, root file systems, data transfer, anything else billed) carries the
most. Where the usage comes from, Fly's billing page read by hand or an
interface Fly documents, is settled when the check is built, and the
runbook names it. Spec 115's F4 makes the first projection before
production exists.

### Failure modes (hosting budget)

- **The projected month exceeds 5 USD.** The cost check reports it, with
  the line that carries most. Nothing stops a machine automatically; the
  operator decides.
- **The first request after a stop waits for the machine to boot.** The
  wait is not measured. A health check or smoke step that times out on a
  cold start fails the deploy; spec 115's F1 measures the start.
- **A Hobby limit is exceeded.** The SPA pauses until 30 days pass. The
  API keeps serving, with no browser client.
- **Fly stops the API machine during a run anyway.** Spec 106: the run
  is recorded interrupted, never silently lost.

### Assumptions (hosting budget)

Each is checked by spec 115 (its "Hosting budget (2026-10-11)", F1 to F5)
and recorded confirmed or refuted there:

- F1, the mechanism spec 106 chooses keeps an auto-stopping machine awake
  while a run is active, and the machine stops once runs end.
- F2, an auto-stop delivers the app's kill signal and waits before the
  machine stops; a machine started again comes up with its root file
  system reset from the image.
- F3, a scheduled machine with restart policy `no` fires about hourly,
  stays stopped between ticks, is not started again while a tick still
  runs, and keeps its schedule through `fly machine update`.
- F4, the projected monthly cost of production under the expected usage
  is under 5 USD, and an app with no machine running, no volume and no
  dedicated IPv4 costs nothing beyond its machines' root file systems.
- F5, 512MB holds the API with Chromium rendering a PDF (only if the
  512MB alternative is taken).

### Honest limitations (hosting budget)

- Supabase Free pauses a project after 1 week of inactivity. Production's
  hourly scheduler tick connects to the database; whether that counts as
  activity is an assumption for spec 115. Staging, stopped between checks,
  may pause and is resumed by hand before a check.
- Supabase Free has no automatic backups, so "Backups of the hosted store
  are Supabase's, per its plan" gives none. The hosted store needs its own
  backup (a scheduled dump to storage outside Supabase, held to spec 030's
  rule that a backup is proven by a restore). That is an open item for this
  spec's implementation, recorded here so it is not assumed.
- The free plans carry limits that end the budget at a growth point, not
  gradually: 500 MB of database, 1 GB of storage, 2 projects.
- The image size, the cold-start time, a tick's running time and the
  data transfer are not measured. Every estimate above that depends on
  them gives its formula, not a total.
- The budget is a target, not a cap. Fly bills what runs; the cost check
  reports, it does not stop anything.
- Hobby's non-commercial restriction binds the whole hosted service while
  the SPA is on Hobby.

### Acceptance criteria (hosting budget)

- [ ] Both Fly files set, for the API's HTTP service,
      `auto_stop_machines = "stop"`, `auto_start_machines = true` and
      `min_machines_running = 0`, a shared-cpu-1x 1GB machine, no
      `[mounts]`, and nothing that allocates a dedicated IPv4 (planned
      test_the_fly_files_run_one_api_machine_that_stops_when_idle).
- [ ] `docs/deploy.md` allocates a shared IPv4 and no dedicated one,
      creates no volume, creates the production scheduler machine with
      `--schedule hourly --restart no` and the staging one without a
      schedule, and puts both Vercel projects on Hobby with the
      non-commercial constraint stated.
- [ ] A monthly cost check projects the month as spent so far / days
      elapsed × days in the month and reports when the projection exceeds
      5 USD, naming the largest line (planned
      test_the_cost_projection_reports_a_month_over_budget), and
      `docs/deploy.md` says where its usage figures come from.
- [ ] Spec 115's F1 to F4 are recorded confirmed or refuted before the
      first production deploy, and a refuted one stops that deploy until
      a spec owns the fix.
- [ ] No text in this spec still requires `auto_stop_machines = "off"` or
      an always-on scheduler machine.

## Region (2026-10-11)

Akin decided in chat on 2026-10-11. This section supersedes Open
decision 2 (Frankfurt: Fly `fra`, Supabase `eu-central-1`) and every
place in this spec that applied it. Specs 106, 108 and 115 point here.

1. **Supabase stays in Ireland, `eu-west-1`.** The existing Supabase
   project named `harrier` was created there, and a Supabase project
   cannot change region. Its reference and URL are written into no spec,
   because this repository is public.
2. **Fly moves to Amsterdam, `ams`.** Fly has no Dublin region. London
   (`lhr`) is outside the EU, and spec 102's open decision 2 chose the EU
   given the target users and GDPR. Amsterdam is the closest Fly region
   to Ireland inside the EU. Both Fly files set `primary_region = "ams"`.
3. **The existing project is staging for now.** Production's project is
   created later, when a Supabase Free slot is free or the plan changes.
   Free allows 2 active projects, and the account already holds another.
   Nothing serves tenants yet. Spec 115 runs against this staging
   project, which already exists in `eu-west-1`. Production keeps its
   names in the environments table (`harrier-production`,
   `harrier-api-production`, `harrier-web-production`); none of them
   exists yet.

### Prices

Fly's pricing page gives a region's price as the base-region price times
a region multiplier listed on that page. Amsterdam's multiplier is
1.038461538; Frankfurt's is 1.153846154 (Fly, Pricing,
https://docs.fly.io/about/pricing, read 2026-10-11). The base-region
shared-cpu-1x prices are 512MB $3.69 and 1GB $6.70 a month.

- 1GB in Amsterdam: 6.70 × 1.038461538 = 6.96 USD a month, which the
  page's region selector shows as $6.95. This spec uses $6.95.
- 512MB in Amsterdam: 3.69 × 1.038461538 = 3.83 USD a month, shown as
  $3.84. This spec uses $3.84.
- The Frankfurt figure this spec used before, $7.73 for 1GB, is
  6.70 × 1.153846154 = 7.73.

Every Frankfurt price and every estimate built on it is replaced in
"Hosting budget (2026-10-11)" above:

| Figure | Frankfurt (superseded) | Amsterdam | Arithmetic |
|---|---|---|---|
| 1GB always on, a month | $7.73 | $6.95 | 6.70 × 1.038461538 = 6.96, shown as 6.95 |
| Over the 5 USD budget by | 2.73 USD | 1.95 USD | 6.95 minus 5 |
| One machine hour, estimate | 0.0107 USD | 0.0097 USD | 6.95 / 720 = 0.00965 |
| Share of a month 5 USD buys, estimate | 0.647 | 0.719 | 5 / 6.95 = 0.719 |
| Machine hours 5 USD buys, estimate | about 465 of 720 | about 518 of 720 | 0.719 × 720 = 517.7 |
| 512MB always on, a month | 4.26 USD, estimated | $3.84 | 3.69 × 1.038461538 = 3.83, shown as 3.84 |
| Left under 5 USD with 512MB | about 0.74 USD | about 1.16 USD | 5 minus 3.84 |

### Honest limitations (region)

- The API in Amsterdam and the database in Ireland are in two cities, so
  every request's database round trips cross between them. No latency
  figure is claimed or measured here; the first staging deploy shows it.
- Production does not exist. Every production step in this spec waits
  for a Free slot or a plan change. Supabase Pro is from 25 USD a month,
  so a plan change alone puts the hosted deployment over the 5 USD
  target (Hosting budget, "What the published prices allow").
- The prices used are the region selector's rounded figures, which
  differ from the multiplied ones by one cent (6.95 against 6.96, 3.84
  against 3.83). Which of the two Fly bills is not checked here.

## Amendment (2026-10-11): login roles reach no role past the policy

The local isolation review on PR #260 (spec 105's migration 16) found
that a request session that can run SQL can leave `harrier_tenant` with
`SET LOCAL ROLE <r>`, for any role the login role is a member of. Postgres
checks `SET ROLE` against the login role's memberships, not the current
role's, and `NOINHERIT` does not stop it. Spec 104's amendment of the same
date ("migration 16's follow-ups") states the mechanism, the check on
Postgres 17.11 that showed it, and the wider start check: a login role
must be a member, directly or through other roles, of no role that is a
superuser, has `BYPASSRLS`, owns a table in `public` or a table with row
security enabled in any other schema, or is `service_role` or `postgres`.

`harrier store grant-tenant` and `check-deploy` call the same function as
spec 104's start checks (Behavior), so the deploy refuses those login
roles with no further change. This section adds what only the deployed
project can show: which roles each login role actually reaches there.

### Behavior (login role memberships)

- **`check-deploy` compares each login role's memberships with the ones
  the deploy grants.** For the API's login role (`harrier_api` in the
  documents), and for `harrier_run` and `harrier_scheduler` once their
  specs' deploy steps create them (amendment of 2026-10-11 above), it
  reads from the deployed project's catalog every role the login role is
  a member of, directly or through other roles
  (`pg_has_role(<login role>, <role>, 'MEMBER')`, any grant options). It
  fails, naming the login role and each role that should not be there,
  unless that set is exactly: `harrier_tenant` for `harrier_api` and
  `harrier_run`; `harrier_tenant` and `harrier_schedule_reader` for
  `harrier_scheduler`. This is stricter than spec 104's rule: a membership
  the deploy did not grant fails whether or not it bypasses row security.
  On a pass it prints one line per login role, `<login role>: member of
  <roles, sorted>`. Role names are not secret; no URL or password is
  printed.
- **A read-only check of the deployed project.** A new module,
  `services/api/tests/test_deployed_login_roles.py`, holds one test
  (planned test_the_deployed_login_roles_reach_only_their_grants). It
  connects with the environment's migrator URL, exported as
  `HARRIER_DEPLOYED_MIGRATE_URL` in the operator's shell and never in
  `.env`, writes nothing, and asserts for each login role above that
  exists: the set of roles it reaches equals the one above, and no role in
  that set, nor the login role itself, is a superuser, has `BYPASSRLS`,
  owns a table in `public` or a table with row security enabled in any
  schema, or is `service_role` or `postgres`. It shares with `check-deploy`
  the function that reads the reached roles, so the CI test below shows
  that function failing. Without the variable it skips naming it, locally
  and in CI. It is a recorded check, not a gate (spec 039); `check-deploy`
  is the gate. Unlike spec 115's staging mode it does not refuse a project
  with users, because it writes nothing, so it runs against production.
- **The runbook** creates each login role with `CREATE ROLE ... LOGIN
  NOINHERIT` and grants it roles only through the deploy's grant steps.
  The operator login that can `SET ROLE service_role` (Auth settings,
  above) is a separate role and is never one of the three login roles.
  A membership `check-deploy` names is removed by the operator with
  `REVOKE`, as the runbook says; the expected sets change only by a spec.

### Failure modes (login role memberships)

- **A login role reaches a role the deploy did not grant.** `check-deploy`
  exits 1 at step 4, naming the login role and the role reached, so no
  image is deployed. The running API keeps serving, and spec 104's start
  check refuses any new machine whose login role reaches a role past the
  policy.
- **The deployed check's URL is unset.** The module skips, naming
  `HARRIER_DEPLOYED_MIGRATE_URL`.

### Honest limitations (login role memberships)

- **Read at deploy, and when the operator runs the module.** A grant made
  between deploys is seen at the next deploy, or at the API's next start
  for a role past the policy (spec 104).
- **Memberships, not every path past a policy.** A `SECURITY DEFINER`
  function owned by a bypassing role is another way past, as spec 104's
  amendment says; neither check reads one outside `public`.
- **Production does not exist yet** (Region, Honest limitations). The
  production half of the criteria below waits for it.

### Acceptance criteria (login role memberships)

- [ ] `check-deploy` fails, naming the login role and the role reached,
      for a login role that is also a member, through a `NOINHERIT`
      intermediate role, of a `BYPASSRLS` role, of a role without
      `BYPASSRLS` that owns a table in `public`, and of a role that
      bypasses nothing; it passes on a migrated, granted store and prints
      each login role's memberships
      (planned test_check_deploy_names_a_login_role_that_reaches_another_role).
- [ ] The deployed-project check exists as above and skips, naming
      `HARRIER_DEPLOYED_MIGRATE_URL`, when it is unset
      (planned test_the_deployed_login_roles_reach_only_their_grants).
- [ ] The pull request that first deploys staging records that check's
      output against the staging project, passing, with role names and no
      URL, host, project reference or password; the first production
      deploy records the same against production.
