# Deploying the hosted service

The hosted service (ADR-013, spec 102) runs in two environments, staging and
production. Vercel serves the SPA, Fly runs the API image, and Supabase holds
the database, the sign-in and the files, all in the EU. This is the runbook for
setting up an environment once and for the deploys after it. Spec 110 is the
design; this file is what an operator types.

Nothing here is run by the repository. Every account, project, secret and token
below is made by hand, by the operator, and none of the commands are run by a
test. The workflow `.github/workflows/deploy.yml` is the only thing that
deploys, and it deploys only what the one-time setup made ready.

What a command prints is never a URL, a key or a password. If one does, stop and
rotate that credential (see "Revoking a credential").

## What deploys what

| | Staging | Production |
|---|---|---|
| Supabase project | `harrier`, `eu-west-1` (exists) | `harrier-production`, `eu-west-1` (not created yet) |
| Fly API app | `harrier-api-staging`, `ams` | `harrier-api-production`, `ams` |
| Fly scheduler app | `harrier-scheduler-staging`, `ams` | `harrier-scheduler-production`, `ams` |
| Vercel project | `harrier`, served at `harrier-web-staging.akinoztorun.dev` | `harrier-web-production` |
| GitHub environment | `staging`, deployments from `main` only | `production`, deployments from `main` only, one required reviewer |

The names are the shape, not a commitment. Another name goes in the Fly
configuration files (`deploy/fly.staging.toml`, `deploy/fly.production.toml`),
in the GitHub environment variables below, and in the Vercel variables.

Staging deploys on every push to `main` whose CI run passed. Production
deploys a commit staging already ran, by hand, after the required reviewer
approves it:

```
gh workflow run deploy.yml -f sha=<the full 40 character commit id>
```

The workflow refuses a commit with no successful staging deployment before the
production environment's secrets are released. The tests that hold this file to
its order are `services/api/tests/test_deploy_files.py::test_the_deploy_workflow_migrates_before_it_deploys`
and `::test_production_takes_a_sha_and_refuses_one_staging_never_deployed`.

## One deploy, in order

1. The image. Staging builds the `hosted` target of the `Dockerfile` once and
   pushes it, tagged with the commit, to the API app's registry and copies it
   to the scheduler app's. Production builds nothing: it copies the image
   staging ran, by digest, into its own two registries.
2. `harrier store migrate`, as the migrator. A refusal stops the deploy and the
   running API is untouched.
3. `harrier store grant-tenant` for the API's, the run's and the scheduler's
   login roles, then `harrier store grant-schedule-reader` for the scheduler's.
4. `harrier store check-deploy`. Exit 1 stops the deploy.
5. `fly deploy --image <digest>`. Each new machine runs the API's start checks
   (spec 104, spec 134); a machine that refuses never passes its health check
   and Fly keeps the old one. Then the machine count: exactly one API machine,
   started or stopped (`harrier deploy check-machine-count`).
6. The scheduler machine moves to the same digest with `fly machine update`,
   never with `--skip-start` (spec 108, amendment of 2026-10-12).
7. Smoke: `GET /health` answers with the commit as its `revision`; the
   Supabase REST endpoint is not a 2xx; the Auth settings are invite only
   (`harrier deploy check-auth-settings`).
8. The SPA: `vercel pull`, `vercel build --prod`, `vercel deploy --prebuilt
   --prod`. Last, so a new SPA never talks to an old API.
9. Production only: `harrier deploy check-scheduler-tick` waits up to 70
   minutes for the scheduler machine's first tick after the update.

**The rule a migration must keep.** Machines of the old release keep serving
from step 2 to step 5, against the new schema. A migration therefore leaves the
previous release working: it adds, and removes only what the previous release no
longer reads. A migration that cannot is deployed with the API scaled to zero
first (`fly scale count 0 --app <api app>`, deploy, `fly scale count 1`), which
is downtime. Nothing checks this rule.

## Where each secret lives

| Value | Lives in | Never in |
|---|---|---|
| The `postgres` role's session-pooler URL (the migrator) | GitHub environment secret `HARRIER_MIGRATE_DATABASE_URL` | Fly, Vercel, `.env`, git |
| `harrier_api`'s URL | Fly secret `HARRIER_DATABASE_URL` on the API app | GitHub, Vercel, `.env`, git |
| `harrier_run`'s URL | Fly secret `HARRIER_RUN_DATABASE_URL` on the API app | GitHub, Vercel, `.env`, git |
| `HARRIER_CREDENTIALS_KEY` | Fly secret on the API app and on the scheduler app | GitHub, Vercel, `.env`, git |
| `harrier_scheduler`'s URL | Fly secret `HARRIER_DATABASE_URL` on the scheduler app (with `HARRIER_RUN_DATABASE_URL`, the run login's, the scheduler starts runs with) | GitHub, Vercel, `.env`, git |
| `HARRIER_STORAGE_SIGNING_KEY`, the private key that mints each scheduled run's Storage token (spec 107) | Fly secret on the scheduler app only | the API app (it refuses to start with it), GitHub, Vercel, `.env`, git |
| A Fly deploy token for the API app | GitHub environment secret `FLY_API_TOKEN` | Fly, Vercel, git |
| A Fly deploy token for the scheduler app | GitHub environment secret `FLY_SCHEDULER_API_TOKEN` | Fly, Vercel, git |
| Production only: a Fly deploy token for the staging API app | GitHub `production` environment secret `FLY_STAGING_API_TOKEN` (it reads staging's image) | Fly, Vercel, git |
| A Vercel token | GitHub environment secret `VERCEL_TOKEN` | Fly, git |
| The Vercel organization and project ids, the Supabase URL, the publishable key, the API's login role names, the app names | GitHub environment variables (listed below), and for the SPA Vercel environment variables | none of them is secret |
| `HARRIER_AUTH`, `HARRIER_ALLOWED_HOSTS`, `HARRIER_CORS_ORIGINS` | `[env]` in the app's Fly configuration file | none of them is secret |
| `HARRIER_SUPABASE_URL`, `HARRIER_SUPABASE_PUBLISHABLE_KEY` | placeholders in `[env]` of the Fly configuration file, filled from the GitHub variables into a copy at deploy (`harrier deploy fill-fly-config`) | a project's real values in git |
| The operator login's URL and `HARRIER_SUPABASE_SECRET_KEY` | the operator's shell, for the one command that needs it | Fly, GitHub, Vercel, `.env`, git |
| The service role key, any `sb_secret_` key | nowhere in the pipeline | Fly (the API refuses to start), GitHub, Vercel, git |

GitHub never holds the API's password, Fly never holds the migrator's, and
neither holds the service role. Fly secrets are app wide, reach every machine of
the app as environment variables, and cannot be read back; that is why the
migrator is not one of them. The test that holds the workflow to this split is
`services/api/tests/test_deploy_files.py::test_each_secret_reaches_only_the_steps_that_need_it`.

The GitHub environment variables, set once per environment with
`gh variable set <NAME> --env <environment> --body <value>`:

| Variable | Value |
|---|---|
| `FLY_APP` | the API app, for example `harrier-api-staging` |
| `FLY_SCHEDULER_APP` | the scheduler app, for example `harrier-scheduler-staging` |
| `FLY_STAGING_APP` | production only: `harrier-api-staging` |
| `HARRIER_API_LOGIN` | `harrier_api` |
| `HARRIER_RUN_LOGIN` | `harrier_run` |
| `HARRIER_SCHEDULER_LOGIN` | `harrier_scheduler` |
| `HARRIER_SUPABASE_URL` | the project's `https://<ref>.supabase.co` |
| `HARRIER_SUPABASE_PUBLISHABLE_KEY` | the project's publishable key (never a secret key) |
| `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID` | from `.vercel/project.json` after `vercel link` |

The secrets, each typed at the prompt so the value never sits on a command line:
`gh secret set <NAME> --env <environment>`.

## One-time setup, per environment

Do these in order. Each step names its check.

### 1. The Supabase project

Production's project is created when a Free slot is free or the plan changes.
Supabase Free allows 2 active projects, and Pro is from 25 USD a month, which
alone exceeds the 5 USD a month target. Create it in `eu-west-1` (a project
cannot change region).

Set each of these in the dashboard, once, and run its check:

| Setting | Check |
|---|---|
| Data API off ("Enable Data API"). With it off, none of the auto generated REST endpoints respond, regardless of grants or row security. | `curl -s -o /dev/null -w '%{http_code}\n' -H "apikey: $PUBLISHABLE_KEY" "$SUPABASE_URL/rest/v1/jobs"` prints a status that is not 2xx. The deploy runs the same probe at step 7. |
| An asymmetric signing key current (the API refuses `HS256`). | `curl -s "$SUPABASE_URL/auth/v1/.well-known/jwks.json" \| jq '[.keys[].alg]'` lists asymmetric algorithms only. |
| Anonymous sign-ins off, and public sign-up off until spec 111. | `HARRIER_SUPABASE_URL=... HARRIER_SUPABASE_PUBLISHABLE_KEY=... uv run --project services/api --no-dev harrier deploy check-auth-settings` prints `auth settings: invite only`. The deploy runs it at step 7. |
| The email template carries `{{ .Token }}`, so sign-in sends a code rather than a link (spec 114), and a sender that reaches invited users. Supabase's built-in sender is not assumed to. The dashboard lets a template be edited only once custom SMTP is on (Authentication, Emails, SMTP Settings); staging sends through Google Workspace's `smtp.gmail.com` on port 465 with an app password. | Invite one address you control (spec 111), ask for a sign-in code on the staging SPA, and read the code in the mail. |
| Redirect URLs name only the environment's SPA origin. | The same sign-in, with a code, and no link to another origin. |

The project's session pooler is the connection path for the migrator and for
every login role: one path, over IPv4, with the session state a migration's
advisory lock needs. A custom role connects to it as `<role>.<project ref>`.
The project's current Postgres major version is the one Supabase created it on;
the CI store runs 17.

Backups: Supabase Free has no automatic backups, and this pipeline adds none. The
hosted store needs its own backup, a scheduled dump held to spec 030's rule that a
backup is proven by a restore; that is an open item, not a done one. **Nothing
backs up the Storage bucket `harrier-files` either, until a spec adds a backup,
and a deleted or overwritten object is gone** (spec 107, Honest limitations).

### 2. The login roles

Four database roles are made by hand, as the migrator, once: the API's, the
run's, the scheduler's, and the operator's. Each of the first three is
`LOGIN NOINHERIT`, and the deploy's grant steps give each only what it needs;
the operator's login is a separate role, never one of the three.

Read the migrator's URL into this shell only, never into `.env` and never as a
typed command, which the shell's history would keep:

```
read -rs HARRIER_DATABASE_URL   # paste the postgres role's session pooler URL, port 5432
export HARRIER_DATABASE_URL
psql "$HARRIER_DATABASE_URL"
```

For each of `harrier_api`, `harrier_run` and `harrier_scheduler`, make the
role, then set its password from a shell variable. Nothing is pasted:

```
API_PW=$(openssl rand -hex 24)
psql "$HARRIER_DATABASE_URL" -c 'CREATE ROLE harrier_api LOGIN NOINHERIT'
V=$(PW="$API_PW" python3 -c 'import os,hashlib,hmac,base64 as b;s=os.urandom(16);k=hashlib.pbkdf2_hmac("sha256",os.environ["PW"].encode(),s,4096);c=hmac.new(k,b"Client Key","sha256").digest();print("SCRAM-SHA-256$4096:"+b.b64encode(s).decode()+"$"+b.b64encode(hashlib.sha256(c).digest()).decode()+":"+b.b64encode(hmac.new(k,b"Server Key","sha256").digest()).decode())')
psql "$HARRIER_DATABASE_URL" -c "ALTER ROLE harrier_api PASSWORD '$V'"; unset V
```

The Python line computes the SCRAM-SHA-256 verifier that `\password` would
send, so only the salted hash reaches the server and no statement or log holds
the password. `openssl rand -hex 24` needs no percent encoding in a URL. Keep
the variable for the Fly secret in step 3, and check the login before you rely
on it: `psql "<its URL>" -Atc 'select current_user'` prints the role's name. A
password never goes through the workflow.

Do not set these passwords by pasting into `\password`. The clipboard holds
whatever was copied last, and copying the next command from a runbook or a
chat replaces the password: on 2026-10-15 `\password` stored the wrong value
twice that way, and the login failed with "password authentication failed".
The same holds for `read -rs`: copy the command first, run it, then copy the
secret and paste it at the waiting prompt.

A password manager keeps the URLs past the terminal that made them. With the
1Password CLI, `op item create --category=password --vault=<vault>
--title=<name> "password=$URL"` stores one, and `op read op://<vault>/<name>/password`
reads it back into the one command that needs it.

Do not grant these roles anything yourself. `harrier store grant-tenant` and
`harrier store grant-schedule-reader` do, and refuse a role that cannot log in,
inherits, bypasses row security, or reaches a role past the policy.

The operator login that `harrier operator erase` uses (spec 111) is a fourth
role that can `SET ROLE service_role`. Its URL and `HARRIER_SUPABASE_SECRET_KEY`
live in the operator's shell for the one command that needs them.

Dropping and remaking a login role needs its pooled sessions ended first, or the
pooler hands the new role an idle connection still logged in as the old one
(spec 115, N3):

```
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = 'harrier_api';
DROP ROLE harrier_api;
```

Check: after the first deploy's grant and check steps, `uv run --project
services/api harrier store check-deploy --login harrier_api --login harrier_run
--scheduler-login harrier_scheduler` (with the migrator URL exported) prints
`harrier_api: member of harrier_tenant` and the same for the run login, and
`harrier_scheduler: member of harrier_schedule_reader, harrier_tenant`.

### 3. The Fly apps

Per environment, two apps in `ams`, one for the API and one for the scheduler:

```
fly apps create harrier-api-staging
fly apps create harrier-scheduler-staging
fly ips allocate-v4 --shared --app harrier-api-staging
fly ips allocate-v6 --app harrier-api-staging
```

A shared IPv4 address is free; a dedicated one is 2 USD a month, 40 percent of
the 5 USD target, so none is allocated. No volume is created: hosted state lives
in Supabase. The scheduler app has no service and no address.

The API's secrets, set from the shell that made the passwords (a value is typed
or taken from a variable, never pasted into a file):

```
fly secrets set --stage --app harrier-api-staging \
  "HARRIER_DATABASE_URL=postgresql://harrier_api.${REF}:${API_PW}@${POOLER_HOST}:5432/postgres" \
  "HARRIER_RUN_DATABASE_URL=postgresql://harrier_run.${REF}:${RUN_PW}@${POOLER_HOST}:5432/postgres" \
  "HARRIER_CREDENTIALS_KEY=key-1:$(openssl rand -base64 32)"
```

`REF` is the project reference and `POOLER_HOST` the pooler host from the
project's connection panel. The same key goes to the scheduler app, since a
scheduled run start decrypts an owner's credentials: read it once into a shell
variable and set it on both apps. No `TELEGRAM_` or `GMAIL_` variable is set
anywhere: the hosted API refuses to start with one (spec 109).

The scheduler's secrets. Its `HARRIER_DATABASE_URL` is a session mode URL (the
session pooler on port 5432, or a direct connection), never the transaction
pooler on port 6543: it holds a session advisory lock for the whole tick and
refuses to start without one. `harrier_run` must not be the scheduler's own
login, and neither the scheduler's environment nor the API's may hold a
service role key.

```
fly secrets set --stage --app harrier-scheduler-staging \
  "HARRIER_DATABASE_URL=postgresql://harrier_scheduler.${REF}:${SCHEDULER_PW}@${POOLER_HOST}:5432/postgres" \
  "HARRIER_RUN_DATABASE_URL=<the run login's URL, as above>" \
  "HARRIER_CREDENTIALS_KEY=<the same key>"
```

**The scheduler's Storage signing key (spec 107).** A scheduled run reaches
its owner's files in Storage with a token the scheduler mints for that owner,
signed with `HARRIER_STORAGE_SIGNING_KEY`. Without the key the scheduler
starts no discovery or academic discovery run, logs `scheduler starts no run
that writes files: HARRIER_STORAGE_SIGNING_KEY is not set` on each tick, and
the report shows the slots unclaimed. Nothing is written anywhere
(`services/api/tests/test_hosted_scheduler.py::test_a_scheduler_without_a_signing_key_starts_no_file_writing_run`).

The key is the project's current signing key (spec 107, Decision taken
(2026-10-16, PR 3)): Supabase imports it as a standby key, and rotating it
in makes Storage accept the scheduler's tokens. The steps:

1. Make the key on your own machine, outside the repository, as a private
   P-256 JWK with a `kid`:

   ```
   uv run --project services/api python -c "import json, uuid; from cryptography.hazmat.primitives.asymmetric import ec; from jwt.algorithms import ECAlgorithm; k = json.loads(ECAlgorithm.to_jwk(ec.generate_private_key(ec.SECP256R1()))); k['kid'] = str(uuid.uuid4()); print(json.dumps(k))" > "$HOME/scheduler-signing-key.jwk"
   chmod 600 "$HOME/scheduler-signing-key.jwk"
   ```

2. In the Supabase dashboard, Project Settings, JWT Keys: create a new
   standby key by importing that JWK. Copy the file with
   `pbcopy < "$HOME/scheduler-signing-key.jwk"` right before you paste it
   into the dashboard, and copy nothing in between: on this machine the
   clipboard does not keep what an earlier command put there. Keep the
   `kid` Supabase shows equal to the file's: the scheduler's tokens name it.
   Then click Rotate key to make it the current key. The earlier key becomes
   previously used, and every session it signed stays valid.
3. Set it on the scheduler app only, from the file, through standard input,
   so the key is in no process's arguments (`ps` would show
   `fly secrets set "NAME=$(cat ...)"` whole). `printf` is a shell builtin,
   so it starts no process either:

   ```
   printf 'HARRIER_STORAGE_SIGNING_KEY=%s\n' "$(cat "$HOME/scheduler-signing-key.jwk")" \
     | fly secrets import --stage --app harrier-scheduler-staging
   ```

4. Delete the file: `rm "$HOME/scheduler-signing-key.jwk"`. Supabase and the
   Fly secret now hold the key; nothing else should.
5. No sooner than one hour and fifteen minutes after the rotation, revoke
   the previously used key in the same dashboard page, so sessions it signed
   can no longer be used.

A later rotation of the scheduler's key repeats steps 1 to 5 with a new
JWK.

The key goes nowhere else. The API refuses to start with it in its
environment
(`services/api/tests/test_hosted_auth.py::test_the_api_refuses_the_storage_signing_key`),
and a run's environment never carries it. Whoever holds it can
act as any owner in Storage and in the API until it is removed from the
project (spec 107, Decision taken (2026-10-16)). With the key set, the
scheduler also needs the project's URL and publishable key in its machine's
environment (below), and refuses to start without them.

One deploy token per app, so the staging token cannot deploy production. Pick an
expiry you will renew:

```
fly tokens create deploy --app harrier-api-staging --expiry <duration>
fly tokens create deploy --app harrier-scheduler-staging --expiry <duration>
```

Each is typed into `gh secret set FLY_API_TOKEN --env staging` and
`gh secret set FLY_SCHEDULER_API_TOKEN --env staging`. For production, also make
a deploy token for the staging API app and set it as `FLY_STAGING_API_TOKEN` in
the `production` environment: production copies staging's image with it.

**The scheduler machine.** It is made once per environment, after the first
deploy has pushed an image to the scheduler app's registry (the first run fails
at the scheduler step with `the scheduler app must hold exactly one machine`;
that is expected, and the API is already deployed). Production's runs hourly,
staging's has no schedule and is started by hand for a check (`fly machine
start`), so it costs its root file system only between checks:

```
# production
fly machine run "registry.fly.io/harrier-scheduler-production@<the digest the run printed>" \
  --app harrier-scheduler-production --name scheduler --region ams \
  --schedule hourly --restart no \
  --vm-size shared-cpu-1x --vm-memory 1024 \
  --env HARRIER_SCHEDULER_CONCURRENCY=<n> \
  --env HARRIER_SCHEDULED_RUN_TIMEOUT_SECONDS=<seconds> \
  --env HARRIER_SUPABASE_URL=<the project URL, the GitHub variable's value> \
  --env HARRIER_SUPABASE_PUBLISHABLE_KEY=<the publishable key, the GitHub variable's value> \
  harrier operator scheduler

# staging: the same, without --schedule hourly
```

The last two are public. Each scheduled run gets them, with its token, to
reach Storage (spec 107). A machine made before them takes them with `fly
machine update <id> --app <scheduler app> --env
HARRIER_SUPABASE_URL=<...> --env HARRIER_SUPABASE_PUBLISHABLE_KEY=<...>`.

The two limits are the operator's choice per environment; this runbook sets
none. The run timeout also bounds what one tick can cost. Then re-run the
failed deploy job, or push a commit.

Cost check: `fly machine run` bills the machine only while it runs, and a
stopped machine costs its root file system. The hosting budget is 5 USD a month
(see "The monthly cost check").

### 4. The Vercel projects

Staging reuses the existing project `harrier` and is served at the custom
domain `harrier-web-staging.akinoztorun.dev`: a CNAME record at the domain's
DNS host points it at the target Vercel shows when the domain is added. The
API accepts browser calls only from the origin `HARRIER_CORS_ORIGINS` names in
`deploy/fly.staging.toml`, so the SPA's domain and that value change together,
and the Supabase Site URL and Redirect URLs name the same origin. The
project's deployment protection is set to all deployments except custom
domains, so the page at the custom domain needs no Vercel sign-in.

Two projects, `harrier-web-staging` and `harrier-web-production`, each on the
free Hobby plan, root directory `apps/web`, framework Vite, Node 22. They serve
the SPA only and run no function. Link each once from a checkout:

```
vercel link
```

`vercel link` writes `.vercel/project.json`, which holds the project and
organization ids and is never in git (`.vercel/` is ignored and classified).
Copy the two ids into the GitHub variables `VERCEL_ORG_ID` and
`VERCEL_PROJECT_ID`.

Set the four variables the SPA's build reads (spec 114) in each project's
Production environment, with that environment's Supabase project and API:

| Variable | Value |
|---|---|
| `VITE_HARRIER_AUTH` | `supabase` |
| `VITE_SUPABASE_URL` | `https://<ref>.supabase.co` |
| `VITE_SUPABASE_PUBLISHABLE_KEY` | the publishable key, never a secret key (the build refuses one) |
| `VITE_HARRIER_API_URL` | `https://harrier-api-staging.fly.dev` (production: its own) |

`apps/web/vercel.json` turns off Vercel's own Git deployments, so only the
workflow deploys; a new SPA otherwise deploys on push whether or not the API did.
`.vercel/` and `apps/web/.env*` are never-in-git.

**Constraint.** The Hobby plan is for non-commercial, personal use under Vercel's
fair use guidelines. Invite only personal use fits it. Charging tenants would
require Vercel Pro, and is out of scope. When a Hobby usage limit is exceeded,
the feature pauses until 30 days pass, so the SPA can be down for up to 30 days
while the API keeps serving.

Create a Vercel token (account settings) and `gh secret set VERCEL_TOKEN --env
<environment>`. It is account wide and stronger than the job needs; Vercel offers
no per project deploy token.

### 5. The GitHub environments

```
gh api --method PUT repos/<owner>/<repo>/environments/staging
gh api --method PUT repos/<owner>/<repo>/environments/production
```

Then, in the repository's Settings, Environments: limit both to deployments from
`main`; add the owner as the one required reviewer of `production`, with
self review allowed (the owner is the only reviewer). Set each environment's
variables and secrets from the tables above.

Merging `deploy.yml` starts a staging run, since it follows a CI run on `main`.
Make the staging environment first, or expect that run to fail and re-run it.

### 6. The first staging deploy, and what it settles

The pull request that first deploys staging records the run, `GET /health` with
the commit's revision, and the REST probe's non-2xx status. It records each of
these as confirmed or refuted, and a refuted one stops the spec that depends on
it until a spec owns the fix:

- The migrator connects from a GitHub hosted runner through the session pooler
  (runners are taken to have no IPv6 route; not confirmed).
- Fly's health check may set `Host` (spec 104).
- The 1GB machine's cold start, and Fly's proxy delivering the run stream
  unbuffered (spec 114).
- F5, only if 512MB is taken: it holds the API with Chromium rendering a PDF
  (spec 115).
- A plain `fly machine update` of the scheduler machine with no schedule does
  not start it (spec 108, Honest limitations).
- Spec 106: whether a process of the same user can read `/proc/<pid>/environ` of
  the API and of sibling runs, and that a rolling deploy replaces the one API
  machine, ending runs in flight.
- Spec 107's assumptions 1 to 9 about the Storage server.
- Spec 108: that the migrating role owns `harrier_schedule_inputs()`, may create
  and grant `harrier_schedule_reader`, and that the image honors `TZ`.
- Spec 109: a bot cannot send to a private chat whose user has not written to it,
  and a Fly machine reaches `api.telegram.org` with no extra egress setting.
- Spec 111: the admin user creation, a banned user's refresh, the operator login's
  `SET ROLE service_role`, and the Auth settings.
- `services/api/tests/test_deployed_login_roles.py` against the project, with the
  migrator URL exported as `HARRIER_DEPLOYED_MIGRATE_URL` in the shell only:
  `uv run --project services/api pytest -s --tb=line services/api/tests/test_deployed_login_roles.py`
  (`--tb=line`, so a failed connection prints no URL).
  Record its output, role names only.

## Erasing the synthetic test owners

Spec 115's staging check leaves owners in `auth.users` with no email,
phone, identity or sign-in. `harrier operator erase-test-owners` finds them
by that rule, reports them, and with `--confirm <n>` erases each through
`harrier operator erase` (specs 111 and 143).

It reads `auth.users`, `auth.identities` and every owner's rows, so it runs
with the migrator's login, the project's `postgres` role. That role can
`SET ROLE service_role`, which the erase needs. A dedicated operator login
that is only a member of `service_role` cannot read those tables, and the
command then exits 1 naming the rule (spec 143, Amendment).

In the shell only, never in `.env`:

```
read -rs HARRIER_DATABASE_URL           # the migrator URL, as in step 2 above
read -rs HARRIER_SUPABASE_SECRET_KEY    # the project's secret key
export HARRIER_DATABASE_URL HARRIER_SUPABASE_SECRET_KEY
export HARRIER_SUPABASE_URL=https://<ref>.supabase.co
harrier operator erase-test-owners               # the report: writes nothing
harrier operator erase-test-owners --confirm <n> # <n> is the count it printed
```

A failure stops at one owner and names the step. Running it again
continues with the owners that remain. Each erase appends one line to
`data/hosted/erasures.jsonl`.

## Importing a local store into an account

`harrier store import-local` copies the local store, `data/tracker.db`, into one
hosted account (spec 141). It copies the rows of `tracks`, `jobs`, `job_events`,
`job_eligibility`, `contacts`, `profile_documents`, `user_config` and
`job_runs`, and nothing else: artifacts and other files move in a later spec. The
account must be empty apart from track 1, which sign-up made. The command
refuses anything else and writes nothing.

Run it once per account, on the operator's machine, in this order:

1. Stop the local container, so nothing owns the local file:
   `just container-down`. The import refuses while it runs. Leave it stopped
   until the import has finished.
2. Make sure the local store is at the latest version. A store one version
   behind is refused with a sentence that says to start the local app once, so
   it migrates, and stop it again.
3. Find the account's user id on the Supabase dashboard's Users page. It is a
   UUID. An email address is not accepted.
4. Read the run login's URL into this shell only, never into `.env` and never as
   a typed command (the same rule as the migrator's URL, step 2 above). The run
   login is `harrier_run`, the session pooler URL on port 5432. The migrator's
   URL is refused, and so is any login that can reach a role past the row
   policy:

   ```
   read -rs HARRIER_IMPORT_DATABASE_URL   # paste harrier_run's session pooler URL
   export HARRIER_IMPORT_DATABASE_URL
   ```

   `HARRIER_DATABASE_URL` is not read by this command, so the shell may hold it
   or not.
5. Run it, with staging's login and the staging account's user id:

   ```
   uv run --project services/api harrier store import-local --owner <user id>
   ```

   For production, do the same with production's run login and the production
   account's user id, once that project exists.
6. Clear the variable: `unset HARRIER_IMPORT_DATABASE_URL`.

A run that works prints one line per table, `tracks: 3 copied` for example, and
then `imported`. It prints no name, owner id or host. A refusal prints one line
on stderr and exits 1; a missing or malformed `--owner` exits 2.

If a run fails, the account is as it was: the copy is one transaction. Fix what
the line names and run it again. A run for an account that already holds rows is
refused, naming the first table that holds any; it never merges.

The command reads the local file read-only and does not change it. SQLite may
leave empty `tracker.db-wal` and `tracker.db-shm` files beside it (spec 141,
Honest limitations). A long transaction suits a store of a few thousand rows; a
hosted connection that times out rolls back, and a larger store needs batching
that the spec does not provide.

The command's tests are in `services/api/tests/test_import_local.py`.

## Writing one account from the command line

After an import, the browser still cannot add a focused track, store a
track's documents or move a job. `harrier --owner <user id> <command>` runs
six commands against one hosted account instead of the local store
(spec 144): `tracks add`, `tracks list`, `profile put`, `profile list`,
`profile check` and `move`. Any other command given `--owner` is refused
before anything opens.

1. Find the account's user id on the Supabase dashboard's Users page, as for
   the import.
2. Read the run login's URL into this shell only, never into `.env` (a line
   there is skipped) and never as a typed command:

   ```
   read -rs HARRIER_ACCOUNT_DATABASE_URL   # paste harrier_run's session pooler URL
   export HARRIER_ACCOUNT_DATABASE_URL
   ```

   The migrator's URL is refused, as for the import.
3. Run the commands, `--owner` before the command and `--track` beside it:

   ```
   uv run --project services/api harrier --owner <user id> tracks add focused-scope --kind focused --label "Focused scope"
   uv run --project services/api harrier --owner <user id> --track focused-scope profile put screening --file PATH
   uv run --project services/api harrier --owner <user id> --track focused-scope profile check
   ```

   `move` takes the job id the web app shows. The import gave every job a new
   id, so a local id names a different job.
4. Clear the variable: `unset HARRIER_ACCOUNT_DATABASE_URL`.

Each command prints what it prints locally, and no line names the owner id,
the URL or the host. The local container may run meanwhile: these commands
never open the local file. `profile put resume_facts` and `profile put
resume_truth` store the shared documents, and either is refused, with
nothing stored, when the edit gives any track's `profile check` a failure it
did not have. `tests/test_account_writes.py` holds each of these.

## A deploy that fails halfway

harrier has no down migrations (spec 090): a bad migration is fixed forward.

| Where it failed | State | What to do |
|---|---|---|
| Migrate | The store stays at its version, no image deployed, the API keeps serving. | Read the printed refusal; fix the migration; push. |
| Grant or check | The store may already be at the new version; old machines keep serving it (the migration rule above). | The step names the role and the check. Fix the role (`ALTER ROLE ... NOINHERIT`, `REVOKE` a membership the step names), then re-run the job. The expected memberships change only by a spec. |
| `fly deploy`, after a migration | The store is ahead of the running release. Running machines keep serving; one that restarts is refused (store ahead) and stays down. | Deploy forward: a fixed image. Never roll the schema back. |
| A new machine refuses to start | Its health check never passes, Fly keeps the old machines, the deploy reports failure. | `fly logs --app <api app>` names the start check that refused. |
| The machine count | More than one API machine. | `fly machines list --app <api app>`, destroy the extra, re-run. A second machine needs `fly-replay` routing first (spec 106). |
| The REST probe returns a 2xx | The Data API is on. The API is live; the SPA is not deployed. | Turn "Enable Data API" off, re-run. |
| The Auth settings check | Sign-up or anonymous sign-ins or a provider is on. | Turn it off in the dashboard, re-run. |
| The SPA step | The API is new and the SPA old. | Re-run the job. |
| The scheduler tick check | The scheduler machine did not start within 70 minutes of the update. | `fly machine start <id> --app <scheduler app>` (it runs one tick), then confirm it ticked. |

The production gate asks GitHub for a successful `staging` deployment of the
commit. For a deploy that follows a CI run, GitHub records the deployment against
the head of `main` when the deploy started, which is the CI run's own commit
unless a newer commit landed first; an older commit then has no record and
production refuses it. Deploy the newer one.

Two deploys of one environment never interleave (a concurrency group per
environment), and two migrations are serialized by an advisory lock in any case.
A commit whose CI run finishes after a newer commit's deploy would deploy over
it; deploy forward again.

## Revoking a credential

| Credential | Revoke |
|---|---|
| A Fly deploy token | `fly tokens list --app <app>`, then `fly tokens revoke <id>`; make a new one and update the GitHub secret. |
| The migrator's URL | Reset the `postgres` role's password in the Supabase dashboard (database settings); update `HARRIER_MIGRATE_DATABASE_URL`. |
| A login role's password | As the migrator, set a new one from a shell variable as in step 2 (`ALTER ROLE ... PASSWORD` with the verifier); end its pooled sessions first (`pg_terminate_backend`); `fly secrets set` the new URL; the machines restart. |
| `HARRIER_CREDENTIALS_KEY` | Rotation is spec 116's, not this pipeline's; setting a new one makes stored credentials unreadable until they are re-entered. |
| `HARRIER_STORAGE_SIGNING_KEY` | Make a new key, import it and rotate it in as above; set it on the scheduler app through standard input (`fly secrets import`, step 3 above). The old key also signed every session while it was current, so revoke it no sooner than one hour and fifteen minutes after the rotation, or every user signs in again. A leaked key is revoked at once: every session it signed ends, and scheduled runs fail at their first file until the new key is set. |
| A Vercel token | Delete it in the account's tokens; make a new one; update `VERCEL_TOKEN`. |
| The publishable key | It is public by design; rotate it in the project's API keys, then update the GitHub variable and the Vercel variable. |

## The monthly cost check

The hosted deployment targets under 5 USD a month. Once a month, read the
month's billed usage so far and project it:

- Fly: the organization's Cost Explorer (Billing, Cost Explorer), the month so
  far, per line.
- Vercel: each project's usage page.

Type the lines in. No Fly or Vercel token joins the pipeline, and the command
reads no network:

```
uv run --project services/api --no-dev harrier deploy project-cost \
  --as-of <the last day the figures cover, YYYY-MM-DD> \
  --line "machine time=<usd>" --line "root file systems=<usd>" \
  --line "data transfer=<usd>" --line "vercel=<usd>"
```

It prints the projection, spent so far / days elapsed x days in the month, names
the largest line, and exits 1 with `over the 5 USD budget` when the projection
exceeds 5 USD, compared before rounding. Nothing stops a machine automatically:
the check reports and the operator decides. The test is
`services/api/tests/test_deploy_checks.py::test_the_cost_projection_reports_a_month_over_budget`.

## Limitations

- The migration rule is a rule, not a check.
- No down migrations: a bad migration is fixed forward.
- GitHub hosted runners are taken to reach the internet over IPv4 only, so the
  migrator uses the session pooler, while Supabase recommends a direct connection
  for migrations. Not confirmed; the first staging deploy shows it.
- Production does not exist yet, and every production step waits for a Free slot
  or a plan change.
- The Vercel token is account wide.
- Fly's log store keeps 7 days and does not say where it runs.
- Supabase Free pauses a project after 1 week of inactivity, and has no
  automatic backups.
- `harrier deploy check-scheduler-tick` reads the machine's events from `fly
  machine status --json`, whose time format is read as epoch milliseconds or an
  ISO time and is not confirmed on a real machine.
- Staging's scheduler machine has no schedule, so staging does not exercise the
  hourly cycle.
- The Fly configuration's placeholders are filled by the deploy, so `fly deploy`
  run by hand against the file as committed fails: run `harrier deploy
  fill-fly-config` first.
- Content Security Policy and other response headers for the SPA are not set
  here (spec 114 leaves them to this spec; none is specified yet).
