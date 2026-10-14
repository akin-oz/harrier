---
spec: 108
title: Hosted scheduling
status: accepted
approved: yes
milestone: M10
depends: [020, 029, 097, 103, 104, 105, 106, 112]
---

# Spec 108: Hosted scheduling

Refined 2026-10-10 after approval of the stub. This text is an amendment
to the approved stub and goes to Akin before implementation.

## Problem

launchd runs the schedule on one Mac (ADR-006). The hosted deployment has no
launchd and many owners. ADR-013 decision 4 has the scheduler list due
owners with the service role and start each run as its owner. It never
executes domain code as the service role.

Today the schedule is a committed file and a renderer. Line numbers are
from `main` at `44ac0d7` (2026-10-10), except the
`harrier_api/app.py` ones, refreshed to `main` at `524dd8a` on 2026-10-11,
the `schema.py` ones, refreshed to `main` at `5c3d083` after migration 16,
the `harrier_cli/main.py` ones, refreshed to `main` at `a7b9998`, and the
`pgstore.py` one, refreshed by PR #245.

- **The cadence.** `config/schedule.json` defines four jobs, counted by
  reading the file: `discovery` four times a day (`:6-16`),
  `academic-discovery` on Mondays (`:19-24`), `digest` daily (`:27-32`) and
  `gmail-watch` every 300 seconds (`:35-37`). `load_schedule` parses it
  (`services/api/src/harrier/schedule.py:232-257`) and `render_plist_dict`
  turns each job into a launchd plist that runs `harrier_cli.main` with
  the job's command (`schedule.py:265-299`). Nothing else starts a
  scheduled run.
- **Time is the machine's.** launchd fires in the Mac's local time. The
  weekday-morning Apify gate, `apify_allowed_now`, reads a naive
  `datetime.now()` and allows Apify when the hour is before 12 on a
  weekday (`services/api/src/harrier/discovery.py:130-133`). The CLI calls
  it with no argument for a `--scheduled` run
  (`services/api/src/harrier_cli/main.py:187-188`). On a hosted machine
  the local time is the machine's, not the owner's.
- **Outcome.** A scheduled run reports through its exit status: 0, or 3
  when every attempted source failed or none was attempted
  (`services/api/src/harrier/runoutcome.py:36`, `:60-75`;
  `harrier_cli/main.py:227-238`). Its last success is a row in `job_runs`,
  written only on success (`runoutcome.py:138-152`). Spec 029's overdue
  rule compares that time with twice the longest gap in the job's cadence
  (`schedule.py:452`, `:494-508`, `:555`). launchd's own record, the last
  exit status, is read by `harrier schedule status`, which exits 1 when a
  job has a problem (`schedule.py:103-120`; `harrier_cli/main.py:2199-2212`).
- **Every job of the installation runs.** There is one person, so there
  is no per-person choice of jobs and no per-person timezone.

Hosted, several things change at once:

- launchd does not exist, and spec 112 refuses `harrier schedule` on a
  Postgres store (spec 112, "The CLI on a Postgres URL", local-only
  class).
- Owners' rows sit under row-level policy (spec 105). Finding which owners
  have work due needs a read across owners, which `harrier_tenant` cannot
  do and must not be able to do.
- Spec 105 gave `service_role` no privilege on any harrier table and said
  that "spec 108's due-owner list" grants what it needs in its own
  migration, where it is reviewed as a bypass (spec 105, Grants).
  Supabase's `service_role` has `BYPASSRLS`, so any table it is granted
  is readable for every owner and every row. `user_config`, where an
  owner's choices would naturally sit, also holds their watchlist and
  searches.
- One owner's run can fail, hang or be refused for a missing credential.
  Launchd isolated jobs by process; a hosted scheduler must isolate owners
  the same way and still report every outcome.
- Each tenant brings their own Apify token (ADR-013 decision 8, spec 102
  open decision 3). A scheduler that starts a slot twice spends the
  tenant's money twice.
- `OWNED_TABLES` is both the catalog's list of owned tables and the input
  to migration 10's statements (`services/api/src/harrier/tracker/schema.py:606-614`
  at `44ac0d7`, used at `:733`, `:754`, `:852` and `:871`). A later owned
  table added to it would change what migration 10 applies to a fresh
  store. (Spec 122 froze migration 10's list, noted 2026-10-11:
  migration 10 now reads `MIGRATION_10_OWNED_TABLES` (`schema.py:691-699`)
  at `:891`, `:912`, `:1010` and `:1030`, and `OWNED_TABLES` (`:703`) adds
  `job_eligibility` to it.)

## Scope

This spec covers the operator's half: the process that starts scheduled
runs, how it finds due work, how each run starts as its owner, how
outcomes are recorded and reported, and the owner's stored choice of jobs
and timezone. The owner's half in the browser is split out into spec 120
(item 12).

**Delivery.** One spec, two pull requests in order. PR 1 is the store:
items 1 to 4. PR 2 is the process and the report: items 5 to 11. PR 1
starts nothing; PR 2 adds no migration.

1. **A new Postgres-only migration (its version is the next free one
   when implemented).** It joins `POSTGRES_MIGRATIONS` and creates the
   table
   `schedule_state`, the role `harrier_schedule_reader` and the function
   `harrier_schedule_inputs()` (Behavior). `MIGRATIONS` gains the same
   version with no statements, declared Postgres-only in
   `SINGLE_DIALECT_MIGRATIONS` with its reason.
2. **Migration 10 keeps its own list.** Migration 10's statements are
   built from a frozen tuple, `MIGRATION_10_OWNED_TABLES`, equal to
   today's `OWNED_TABLES`. `OWNED_TABLES` stays the catalog's list and
   gains `schedule_state`. This is a refactor of how migration 10 is
   written; the statements it produces do not change, and a test holds
   them byte for byte.
3. **Hosted-only tables are declared.** `schema.py` gains
   `HOSTED_ONLY_TABLES: dict[str, str]`, table to reason. The parity test
   removes these tables from the Postgres side before comparing table
   sets, and fails when a declared table does not exist on Postgres or
   exists on SQLite. The catalog test
   (`services/api/tests/test_owner_policy.py::test_every_table_is_owned_or_declared`)
   gains `schedule_state` in its expected grants and
   `harrier_schedule_inputs` in its declared definer functions, whose
   only member today is `harrier_new_owner` (`test_owner_policy.py:386`).
   Items 2 and 3 change spec 105's catalog (Changes to approved specs).
4. **A hosted configuration kind, `schedule`.** The owner's choice of
   jobs and timezone is a `user_config` row of kind `schedule`, validated
   by the store (Behavior). It is a kind only on a Postgres connection
   (spec 112's `Connection.dialect`). On SQLite it stays an unknown kind,
   so the local product does not change. The `/config` routes serve it
   hosted through the kinds the connection allows. The contract does not
   change: `kind` is a path string and `ConfigOut` already carries any
   kind (`services/api/src/harrier_api/app.py:1290-1321`).
5. **A new module, `services/api/src/harrier/hostedschedule.py`,** holds
   the hosted job set, the due computation, the claim and finish writes,
   and the outcome classes. It names no tenant in SQL (ADR-013 decision
   2).
6. **The scheduler process,** `harrier operator scheduler`: one tick
   per start that holds a singleton lock, lists due work through
   `harrier_schedule_inputs()`, claims each slot as its owner, starts
   each run through spec 106's run binding, waits for its runs, and exits
   (Behavior). This text first made it a long-running loop; the hosting
   budget of 2026-10-11 made it one tick per start (Hosting budget
   (2026-10-11)).
7. **The operator's report,** `harrier operator schedule-report`: one
   line per owner and job, exit 1 when anything needs the operator
   (Behavior).
8. **An operator class in the CLI's Postgres gate.** Spec 112's gate
   runs only `harrier store` on a Postgres URL. `harrier operator` runs
   only on a Postgres URL and refuses a SQLite store. It is one command
   group: this spec's `scheduler` and `schedule-report` and spec 111's
   `invite` and `erase` are its subcommands, and each declares the
   database role it runs as (decision D2, 2026-10-11). Spec 112 carries
   the class as its Amendment (2026-10-11).
9. **The API refuses a login that can list due work.** Spec 104's start
   check of the API's login role takes one more refusal: a member of
   `harrier_schedule_reader`. Spec 104 takes an Amendment note (Changes
   to approved specs).
10. **ADR-013 decision 4 gains an "Amended (spec 108)" note** with the
    text under Behavior, "The service-side role".
11. **Documents.** `docs/architecture.md`'s hosted section names the
    scheduler, its role, the kind and this spec.
12. **Findings that belong to other specs are written into them** (specs
    only, no code):
    - Spec 106, approved, takes the run binding requirements under
      Behavior, "What this spec needs from spec 106's run binding"
      (Changes to approved specs).
    - Spec 110, approved, takes the scheduler's deploy lines under
      Behavior, "What spec 110 provisions", and four staging checks
      (Honest limitations) (Changes to approved specs).
    - Spec 120, "The owner sees and sets their schedule", a stub created
      with this refinement, `approved: no`, with the headline under Out of
      scope. `specs/README.md`'s M10 line lists it.

Spec 107 is no longer a dependency. The stub listed it for run state in
Storage; this spec stores no run output (Open decision 6).

Classification: the new module and test files sit under
`services/api/src/` and `services/api/tests/`, which
`config/data-classification.json` already classifies as public. The new
stub, spec 120, sits under `specs/`. No new directory is added. Fly
configuration files are spec 110's and are classified there.

## Behavior

### Where the scheduler runs

`harrier operator scheduler`, on a Fly scheduled machine built from the
API image, started on Fly's `hourly` cycle. Each start runs one tick and
exits (Hosting budget (2026-10-11), which replaced the long-lived process
in its own process group that this paragraph first described). The image
already carries `config/schedule.json` (`Dockerfile:74`). The machine
has no service, so Fly's proxy routes it no request. It runs as its own
database login, never the API's (Options weighed).

### The hosted job set

`HOSTED_JOBS` in `hostedschedule.py` names the jobs a hosted owner may
choose: `discovery` and `academic-discovery`. Each keeps its committed
cadence and command from `config/schedule.json`. `digest` joins when spec
109 gives owners their own Telegram settings, and `gmail-watch` when a
hosted Gmail spec exists. A job outside the set is refused at the write
of the `schedule` kind, and skipped with a log line if the committed
file names one the set does not.

Every owner shares the committed cadence. An owner chooses which hosted
jobs run and in which timezone. Per-owner times are not offered (Open
decision 4).

### The owner's choice: kind `schedule`

```json
{ "timezone": "Europe/Amsterdam", "jobs": ["discovery", "academic-discovery"] }
```

The store refuses, naming the field:

- a value that is not an object, or an object with a field other than
  `timezone` and `jobs`;
- a `timezone` that `zoneinfo.ZoneInfo` cannot load;
- a `jobs` entry outside `HOSTED_JOBS`, or a repeated entry.

`jobs` may be empty, which pauses the owner's schedule. No row means the
owner is not scheduled. There is no file fallback hosted (ADR-009
decision 2). `DELETE /config/schedule` removes the row and stops the
owner's scheduled runs from the next tick.

A change takes effect from the next slot: a slot is due only when it is
at or after the row's `updated_at`. Enabling discovery at 15:00 starts
nothing until 16:00. The owner is never charged for a slot that passed
before they chose it.

### The store

On Postgres only:

```sql
CREATE TABLE schedule_state (
    owner_id uuid NOT NULL DEFAULT auth.uid() REFERENCES auth.users (id),
    job TEXT NOT NULL,
    last_slot_at TEXT NOT NULL,
    last_started_at TEXT,
    last_finished_at TEXT,
    last_outcome TEXT NOT NULL CHECK (last_outcome IN
        ('running', 'ok', 'failed', 'crashed', 'timed_out',
         'not_configured', 'refused', 'interrupted', 'missed')),
    last_exit INTEGER,
    last_ok_at TEXT,
    PRIMARY KEY (owner_id, job)
);
```

- Timestamps are ISO 8601 UTC text, the format `job_runs` uses
  (`schema.py:242-245`) and spec 112 supplies from Python. Two values in
  this format order correctly as text.
- Row security is enabled and forced, with the `owner_only` policy
  `FOR ALL TO harrier_tenant` that every owned table has (spec 105,
  Amendment). The primary key leads with `owner_id`, which is the owner
  index the catalog requires.
- `harrier_tenant` gets `SELECT, INSERT, UPDATE`. No grant includes
  `DELETE` or `TRUNCATE`; deleting an owner's state is spec 111's.
  `PUBLIC`, `anon`, `authenticated` and `service_role` get nothing.
- The table holds job names, times, outcome classes and exit codes. It
  holds nothing a run found.

It is declared in `HOSTED_ONLY_TABLES`: "per-owner scheduler state
(spec 108); launchd keeps the local equivalent (ADR-006)".

### The service-side role

ADR-013 decision 4 gains this note:

> Amended (spec 108). "The service role" for the scheduler is
> `harrier_schedule_reader`, a harrier-owned role with no login, not
> Supabase's `service_role`. Its only reach past the owner policy is
> `EXECUTE` on one function, `harrier_schedule_inputs()`, which returns
> each owner's schedule choice and scheduler state and nothing else.
> Supabase's `service_role` would have read whole tables for every owner,
> `user_config` included, whose other kinds hold an owner's searches and
> watchlist. Column grants cannot narrow a read to one kind.

The migration:

- creates `harrier_schedule_reader NOLOGIN NOINHERIT NOBYPASSRLS` when
  absent, tolerating a concurrent creator, as migration 10 does for
  `harrier_tenant`;
- creates the function below, owned by the migrating role;
- revokes `EXECUTE` on it from `PUBLIC`, `anon`, `authenticated`,
  `service_role` and `harrier_tenant`, and grants it to
  `harrier_schedule_reader` alone.

```sql
CREATE FUNCTION harrier_schedule_inputs()
RETURNS TABLE (
    owner_id uuid, schedule TEXT, schedule_updated_at TEXT, job TEXT,
    last_slot_at TEXT, last_started_at TEXT, last_finished_at TEXT,
    last_outcome TEXT, last_exit INTEGER, last_ok_at TEXT)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
    SELECT ...  /* the join described below */
$$;
```

It joins `public.user_config` rows of kind `schedule` to
`public.schedule_state` on `owner_id`, keeping owners present in either.
It reads no other kind and no other table. It is the one definer function
besides `harrier_new_owner()`.

It reads past the policy only because its owner bypasses row security.
On Supabase the migrating role, `postgres`, has `BYPASSRLS` (spec 105,
Amendment); a migrating role without it would make the function return
nothing. The scheduler checks this at start (below).

### The scheduler's login

The scheduler connects as its own login role, called `harrier_scheduler`
in the documents, `NOINHERIT`, a member of `harrier_tenant` and
`harrier_schedule_reader` and of nothing else. At start it refuses to run,
exit 1, naming the check, when:

- its login is a superuser, has `BYPASSRLS`, owns a table in `public`, or
  is a member, directly or through another role, of `service_role` or
  `postgres`;
- it cannot `SET ROLE harrier_tenant` or `SET ROLE harrier_schedule_reader`;
- the owner of `harrier_schedule_inputs()` has neither superuser nor
  `BYPASSRLS` (the function would see no owner);
- its environment holds `SUPABASE_SERVICE_ROLE_KEY` or a value beginning
  `sb_secret_`, as spec 104 refuses for the API;
- the store is not at `target_version()` (`pgstore.py:84-88`);
- `HARRIER_SCHEDULER_CONCURRENCY` or `HARRIER_SCHEDULED_RUN_TIMEOUT_SECONDS`
  is missing or not a positive integer.

Both limits are required, with no default: what a run may cost in machine
time is the operator's decision per environment, and spec 110 sets them.

The API's login refuses to start when it is a member of
`harrier_schedule_reader` (item 9). The listing is not reachable from any
HTTP route, which is ADR-013 decision 3's rule for the service role.

### One scheduler at a time

The scheduler holds a session-level advisory lock,
`pg_try_advisory_lock(hashtext('harrier.scheduler'), 0)`, on a dedicated
connection for its whole life, which since the hosting budget is one
tick. A session-level advisory lock is held until released or the
session ends (PostgreSQL, Explicit Locking). The two-key form never
meets spec 103's one-key migration lock.

- Without the lock, an instance stands by: it lists nothing, starts
  nothing, logs `scheduler standing by: another instance holds the lock`
  once, and exits 0. The next start tries again. (Before the hosting
  budget it retried each tick in a loop.)
- The lock needs a session that outlives a transaction. Supabase's
  transaction-mode pooler does not support session advisory locks; a
  direct connection or the session-mode pooler does (Supabase, Connecting
  to Postgres). The scheduler's database URL names one of those. A lock
  attempt that cannot be held across ticks fails the start.
- When the lock connection fails, the scheduler terminates its children,
  records each as `interrupted`, logs `scheduler lost its lock`, and exits
  1. The machine's restart policy is `no`, so it stays stopped until its
  next scheduled start (Hosting budget), which sweeps any row left
  `running`. Before the hosting budget, Fly restarted it.

The lock keeps two schedulers from running children at once and from
sweeping each other's runs. It is not what prevents a slot from starting
twice: the claim does that, lock or no lock.

### A tick

Once per start, while holding the lock (every 60 seconds in a loop
before the hosting budget; the start of each tick is now Fly's hourly
cycle):

1. **List.** In a transaction that runs `SET LOCAL ROLE
   harrier_schedule_reader` first, select every row of
   `harrier_schedule_inputs()`. No claims are set. No other statement
   runs in this transaction.
2. **Compute.** In Python, for each owner with a valid `schedule` row and
   each job in it: the job's latest slot at or before now, computed from
   the committed cadence in the owner's timezone. A slot is due when it is
   later than `last_slot_at` (or there is no state row), at or after the
   row's `updated_at`, and the owner has no scheduled run in progress. A
   row that fails validation is skipped and logged with the owner's id and
   the field, never the value.
3. **Queue.** Due slots join an in-memory queue, ordered by slot time,
   then by `last_started_at` with never-started first, then by owner id.
   An owner and job already queued is not queued twice.
4. **Start.** While fewer than `HARRIER_SCHEDULER_CONCURRENCY` scheduled
   runs are running, take the head of the queue and start it (below).
5. **Exit.** When the queue is empty and every run it started has
   finished, timed out or been refused, and each outcome is recorded, the
   process exits 0. A slot that falls due while it runs is listed by the
   next start. (Added by the hosting budget.)

The listing is the only cross-owner read. Every other statement the
scheduler runs is in a transaction as one owner.

### The slot

A slot is an instant: a cadence time on a calendar day in the owner's
timezone, converted to UTC. The cadence's `weekday` uses launchd's
numbering, 1 for Monday (`schedule.py:67-69`), which is Python's
`isoweekday`.

- A local time that does not exist on a day the clocks go forward is the
  instant `zoneinfo` gives for `fold=0`. A local time that occurs twice is
  started once, at the `fold=0` instant.
- After a gap in which the scheduler did not run, only the latest slot
  of each job is due. Earlier slots in the gap are not started, as launchd
  starts a missed calendar job once on wake. A scheduler outage never
  multiplies an owner's runs.

### Claiming and starting a run as its owner

For the head of the queue, in a transaction that first runs `SET LOCAL
ROLE harrier_tenant` and sets `request.jwt.claims` to `{"sub": <owner>,
"role": "authenticated"}`, the request transaction's shape (spec 104):

1. **Recheck.** If the job's latest slot is now later than the queued
   slot, the queued slot is claimed as `missed` and the newer slot is
   queued. Nothing starts.
2. **Claim.** `UPDATE schedule_state SET last_slot_at = %s,
   last_started_at = %s, last_finished_at = NULL, last_outcome =
   'running', last_exit = NULL WHERE job = %s AND last_slot_at < %s`. When
   no row changed, `INSERT` the row. A unique violation means the row
   exists at this slot or later: the slot is someone else's, and nothing
   starts. This is spec 112's update-then-insert shape and names no
   tenant.
3. **Commit,** then start the run through spec 106's binding with the
   owner, the job's command from `config/schedule.json`, and `TZ` set to
   the owner's timezone.

The run's own database session is spec 106's. The scheduler's claim
transaction ends before the child starts, so the scheduler holds no
owner's claims while a run executes.

`TZ` in the child's environment makes a naive `datetime.now()` return the
owner's local time, so `apify_allowed_now` (`discovery.py:130-133`) opens
Apify on the owner's weekday mornings without a code change in discovery.
The academic job's weekday is the owner's for the same reason.

### Finishing a run

When the child exits, or is stopped, the scheduler records the outcome in
a transaction as the owner:

`UPDATE schedule_state SET last_finished_at = %s, last_outcome = %s,
last_exit = %s[, last_ok_at = %s] WHERE job = %s AND last_slot_at = %s`

The `last_slot_at` condition means a finish never overwrites a later
slot's claim.

| What happened | `last_outcome` | `last_exit` | `last_ok_at` |
|---|---|---|---|
| exit 0 | `ok` | 0 | set to the finish time |
| exit 3, spec 029's total failure (`runoutcome.py:36`) | `failed` | 3 | unchanged |
| any other exit status | `crashed` | the status | unchanged |
| still running at `HARRIER_SCHEDULED_RUN_TIMEOUT_SECONDS` | `timed_out` | null | unchanged |
| binding refused: the owner lacks a credential the job needs | `not_configured` | null | unchanged |
| binding refused for any other reason | `refused` | null | unchanged |
| running when a scheduler starts holding the lock | `interrupted` | null | unchanged |
| a newer slot arrived before this one started | `missed` | null | unchanged |

- A timed-out run is terminated, then killed after the grace the run
  manager uses (`services/api/src/harrier_api/runs.py:567`, `:677-685`),
  so its remote work stops charging where the run's own code handles
  termination (spec 097, "Harrier's own wait ends first").
- `last_ok_at` changes only on `ok`, the rule `record_success` keeps for
  `job_runs` (`runoutcome.py:138-144`). The run still writes its own
  `job_runs` success as today, as its owner.
- `interrupted` is not a failure, as spec 041 says of the run manager's
  `interrupted` (`runs.py:31-36`). On acquiring the lock, the scheduler
  marks every `running` row `interrupted`, owner by owner, each in a
  transaction as that owner. The slot is not started again; the next slot
  is.
- The child's standard output and error are read and discarded. Nothing a
  run prints reaches the scheduler's logs or the store (Open decision 6).

### Isolation

- Every owner's run is its own process, started with its own owner's
  environment by spec 106's binding. A run's exit, crash, hang or refusal
  is recorded for that owner and job, and the loop goes on.
- Every per-owner step (claim, start, finish, sweep) catches every
  exception, records the outcome it can, logs it with the owner's id and
  the exception's class, and continues with the next owner. An exception
  in the listing ends that tick only; the next tick lists again.
- At most one scheduled run per owner runs at a time, whatever the job,
  so two scheduled runs never write one owner's seen state at once. Runs
  an owner starts from the browser (spec 106) are not counted here.
- A hung run holds one concurrency slot until its timeout. With
  `HARRIER_SCHEDULER_CONCURRENCY` above 1, other owners keep running
  meanwhile; at 1, they wait for the timeout and no longer.

### Logs

The scheduler logs one line when it starts a run and one when it records
an outcome:

`scheduled start owner=<uuid> job=<name> slot=<iso>`
`scheduled end owner=<uuid> job=<name> slot=<iso> outcome=<class> exit=<n|->`

The owner appears as their auth user id, an opaque uuid. No email, name,
search term, company, argument or run output is logged. Exceptions are
logged by class and a message passed through `scrub_secrets`
(`services/api/src/harrier/sources/__init__.py:27-43`, extended by spec
112 to Postgres URLs).

### The operator's report

`harrier operator schedule-report` runs as `harrier_scheduler`, lists
through `harrier_schedule_inputs()` in a transaction as
`harrier_schedule_reader`, and reads the lock's holder from `pg_locks`.
It prints:

- `scheduler: a tick is running` when a session holds the lock, or
  `scheduler: no tick running` (since the hosting budget; this line first
  read `holding the lock` or `not running`, which assumed a loop that is
  always up);
- one line per owner and job: owner id, job, cadence in words
  (`describe_cadence`, `schedule.py:477-491`), last slot, last outcome,
  exit, last ok time, and a problem when there is one.

A problem is any of the following. "No scheduler holds the lock" was one
until the hosting budget; with one tick per start, no lock is held for
most of each hour, and a scheduler that stopped starting shows as the
unclaimed slot below.

- the last outcome is `failed`, `crashed`, `timed_out` or `refused`;
- the job is overdue: its last `ok` is older than twice its longest gap,
  spec 029's rule (`schedule.py:452`, `:494-508`), or it has never been
  `ok` and the owner chose it longer ago than that;
- a whole slot passed unclaimed: the job's latest slot is older than its
  longest gap and later than `last_slot_at`, which means the scheduler is
  not keeping up or not running.

`not_configured`, `interrupted` and `missed` are shown and are not
problems on their own; a job that keeps missing becomes overdue. The
command exits 1 when any problem exists and 0 otherwise, as
`harrier schedule status` does (spec 040). The operator runs it as a
one-off machine in the app that holds the scheduler, which receives that
app's secrets, for example `fly machine run <image> --rm` with the
command. The scheduler machine itself is stopped between ticks, so `fly
ssh console` on it, as this line first said, does not reach a running
process.

### The CLI on each store

- On a Postgres URL, `harrier operator scheduler` and `harrier operator
  schedule-report` run. Spec 112's gate takes a class beside "local
  only" and "everything else": operator commands, which run only on
  Postgres. Spec 112's Amendment (2026-10-11) carries it, with spec 106's
  run class; these are the only two classes beyond `harrier store`
  (decision D2).
- Each operator subcommand declares the database role it runs as. Both
  of this spec's run as the login `harrier_scheduler`, which takes
  `harrier_schedule_reader` for the listing transaction and
  `harrier_tenant` with one owner's claims for every other transaction
  (The scheduler's login, A tick). Spec 111 declares `invite` and
  `erase`.
- On a SQLite store, both refuse before opening anything, exit 1:
  `error: harrier operator <command> runs only against a hosted store;
  locally the schedule is launchd (harrier schedule)`.
- `harrier schedule` stays local only (spec 112). Nothing about launchd
  changes.

### What this spec needs from spec 106's run binding

Asked of spec 106 (Changes to approved specs):

- A caller other than the API, the scheduler, can start a bound run with
  an owner id, a run command, and extra environment limited to `TZ`.
- The command set includes `discover --scheduled` and `discover
  --scheduled --configured-tracks`, so spec 112's "run command, owner
  required" class covers `discover`.
- `TZ` is in the child's environment allowlist.
- A refusal is typed, and says whether a credential the command needs is
  missing. Its text names the credential's kind, never its value.
- The caller gets a handle it can wait on for the exit status and can
  terminate.

### What spec 110 provisions

Asked of spec 110 (Changes to approved specs):

- A Fly scheduled machine, `--schedule hourly`, restart policy `no`, no
  service, running `harrier operator scheduler` (one tick per start),
  moved to each deploy's image. This replaced, on 2026-10-11 by the
  hosting budget, a process group with one always-on machine that
  restarted on exit. Which app holds it is spec 110's Open decision 10.
- The login role `harrier_scheduler`, granted `harrier_tenant` and
  `harrier_schedule_reader` as named deploy steps, with a direct or
  session-mode database URL.
- `HARRIER_SCHEDULER_CONCURRENCY` and
  `HARRIER_SCHEDULED_RUN_TIMEOUT_SECONDS` per environment.
- The staging checks listed under Honest limitations.

### Cost

Each tenant pays for their own Apify runs with their own token (ADR-013
decision 8). What this spec bounds is how often a schedule starts them:

- At most one start per owner, job and slot, enforced by the claim.
- No catch-up: after an outage, only the latest slot of each job runs.
- A schedule change applies from the next slot.

With the committed cadence, the Apify gate opens on one discovery slot
per weekday, 09:00, the only time before 12:00 (`config/schedule.json:11-14`,
`discovery.py:133`). So a schedule starts at most five LinkedIn Apify
runs per owner per week, counted as five weekdays times one slot. The
academic job starts once per week (`config/schedule.json:23`), and each
of its runs covers every configured academic track in turn (spec 097).

What one run may spend is set where it is today: the LinkedIn count is
clamped at `APIFY_MAX_COUNT` (`discovery.py:82`, `:115-119`), and each
academic track's run carries `max_charge_usd`, at most
`ACADEMIC_MAX_CHARGE_USD` (`services/api/src/harrier/sources/apify_academic.py:127-128`,
`:173-178`; spec 097). This spec adds no per-owner money ceiling (Open
decision 5). An owner can set a usage limit on their own Apify account,
at which Apify suspends the account's platform services (Apify, Billing).

The operator's cost is machine time, bounded by the concurrency limit and
the run timeout. Since the hosting budget it is only the time each tick
runs, plus the stopped machine's root file system; spec 110's "Hosting
budget (2026-10-11)" holds the rates and the monthly check.

## Failure modes

- **An owner's run fails, crashes or hangs.** Recorded for that owner and
  job as `failed`, `crashed` or `timed_out`; reported as a problem by
  `schedule-report`; the next owner's run starts as it would have.
- **An owner lacks a credential the job needs.** The binding refuses
  before anything runs. Recorded `not_configured`, every slot until the
  owner adds it. Not billed, not a problem, not retried within the slot.
- **Two slots of one job overlap a long run.** The second is not started
  while the first runs; when the third arrives, the second is recorded
  `missed`.
- **The scheduler is down across a slot.** On return, only the latest
  slot of each job starts. The report shows the gap while it lasts, as
  "a whole slot passed unclaimed".
- **Two scheduler instances.** One holds the lock; the other stands by.
  If both could start, the claim still lets one slot start once.
- **A deploy or crash kills running children.** Their rows stay
  `running`. The next holder of the lock marks them `interrupted`. The
  slots do not run again.
- **The lock connection drops mid-run.** The scheduler terminates its
  children, records them `interrupted`, and exits 1. Its next scheduled
  start runs the next tick. A child that
  finished first keeps its true outcome, because a finish writes only its
  own slot.
- **An invalid `schedule` row.** Refused at the write. A row that reaches
  the store another way is skipped at each tick, logged with the owner's
  id and the field, and that owner's jobs become overdue in the report.
- **The migrating role cannot bypass row security.** The function would
  return no owner, and nothing would ever be due. The scheduler refuses to
  start with `harrier_schedule_inputs() is owned by a role that does not
  bypass row security; it would list no owner`.
- **The scheduler's login has too much or too little.** Refused at start,
  naming the check (The scheduler's login).
- **The API's login holds `harrier_schedule_reader`.** The API refuses to
  start (item 9). The cross-owner listing never sits behind an HTTP route.
- **`harrier_tenant` calls the function.** Refused with `permission
  denied`: it has no `EXECUTE`, and a `NOINHERIT` login that switched to
  it has only its privileges.
- **The transaction pooler.** A session advisory lock does not survive
  it. The start fails rather than running without a lock it cannot keep.
- **A queue longer than the morning.** With many owners in one timezone
  and a low concurrency limit, a 09:00 slot can start after 12:00 local,
  and the Apify gate is then closed for that run. The run still happens
  without Apify, and its start time shows it in the report. Sizing the
  limit is the operator's. Since the hosting budget, a slot also waits
  for the next hourly start, which Fly gives as approximate.
- **The machine's tz database lacks the owner's zone.** The store refuses
  the zone at the write when `zoneinfo` cannot load it. A child whose C
  library cannot load `TZ` would fall back to UTC silently; spec 110's
  staging check covers the image (Honest limitations).

## Acceptance criteria

The store (PR 1)

- [ ] `POSTGRES_MIGRATIONS` has the new version with `schedule_state`,
      `harrier_schedule_reader` and `harrier_schedule_inputs()` as under
      Behavior; `MIGRATIONS` has the same version with no statements, and
      `SINGLE_DIALECT_MIGRATIONS` declares it Postgres-only with a reason
      (`services/api/tests/test_postgres_store.py::test_every_new_migration_declares_both_dialects`
      passes).
- [ ] Migration 10's statements are identical before and after this
      change, compared as text
      (`services/api/tests/test_owner_policy.py::test_migration_10_statements_do_not_change`).
- [ ] `HOSTED_ONLY_TABLES` declares `schedule_state`;
      `services/api/tests/test_dialect_parity.py::test_both_dialects_build_the_same_tracker`
      passes, and fails when a declared table does not exist on Postgres
      (`services/api/tests/test_dialect_parity.py::test_a_hosted_only_table_naming_nothing_fails`).
- [ ] The catalog test passes with `schedule_state` owned and policed,
      its grants as under Behavior, and `harrier_schedule_inputs` as the
      second declared definer function
      (`services/api/tests/test_owner_policy.py::test_every_table_is_owned_or_declared`,
      `services/api/tests/test_owner_policy.py::test_the_catalog_names_each_breach`).
- [ ] For `schedule_state`, owner A reads none of owner B's rows and
      cannot write as B, as for every owned table
      (`services/api/tests/test_owner_policy.py::test_owner_a_reads_nothing_of_owner_b`,
      `services/api/tests/test_owner_policy.py::test_owner_a_cannot_write_as_owner_b`).
- [ ] As `harrier_schedule_reader`, `harrier_schedule_inputs()` returns
      the schedule and state of two owners and no column of any other
      kind or table; as `harrier_tenant`, `anon`, `authenticated` and
      `service_role`, calling it is refused
      (`services/api/tests/test_hosted_schedule.py::test_only_the_reader_lists_schedules`).
- [ ] The `schedule` kind is accepted on a Postgres connection with a
      valid value, refused naming the field for each case under Behavior,
      and is an unknown kind on SQLite with today's message
      (`services/api/tests/test_hosted_schedule.py::test_the_schedule_kind_is_hosted_only`).
- [ ] `just contract` shows no diff.

The process and the report (PR 2)

- [ ] For a fixed clock and a timezone, the due slot of each hosted job
      is the latest cadence instant at or before now in that zone,
      including a weekday job, a day the clocks go forward and a day
      they go back (planned test_the_due_slot_is_local_to_the_owner).
- [ ] A slot before the `schedule` row's `updated_at` is not due
      (planned test_a_new_choice_starts_at_the_next_slot).
- [ ] After a gap of several slots, only the latest slot of each job
      starts (planned test_an_outage_starts_only_the_latest_slot).
- [ ] Claiming one slot twice, from two connections as the same owner,
      starts it once; a finish for an older slot leaves a newer claim
      unchanged (planned test_a_slot_starts_once).
- [ ] With a fake binding and four owners whose runs exit 3, exit 1,
      hang past the timeout, and exit 0, the fourth owner's run starts and
      finishes `ok` and the first three are recorded `failed`, `crashed`
      and `timed_out`; the hung child is no longer running
      (planned test_one_owner_failing_does_not_stop_another).
- [ ] A binding refusal for a missing credential is recorded
      `not_configured`; any other refusal is `refused`
      (planned test_a_refused_run_is_recorded).
- [ ] No two scheduled runs of one owner run at once; a slot that waited
      past the next slot is recorded `missed`
      (planned test_one_owner_runs_one_scheduled_run_at_a_time).
- [ ] With the lock held by another session, the scheduler starts
      nothing; on acquiring it, `running` rows become `interrupted`
      (planned test_a_second_scheduler_stands_by).
- [ ] Every statement the scheduler runs outside the listing transaction
      runs with `current_user` `harrier_tenant` and one owner's claims;
      the listing transaction sets no claims
      (planned test_the_scheduler_acts_as_each_owner).
- [ ] The scheduler refuses to start for each check under "The
      scheduler's login", naming it
      (planned test_the_scheduler_refuses_a_login_that_can_bypass).
- [ ] The API refuses to start when its login is a member of
      `harrier_schedule_reader` (planned test_the_api_login_cannot_list_schedules).
- [ ] The child's environment carries the owner's `TZ`, and a child
      started with it reads the owner's local hour, so the Apify gate
      opens on the owner's weekday mornings
      (planned test_a_scheduled_run_reads_the_owners_clock).
- [ ] No scheduler log line contains anything but the fields under Logs;
      a run's output appears in no log
      (planned test_scheduler_logs_name_only_the_owner_id).
- [ ] `schedule-report` exits 0 with nothing wrong, and 1 for each
      problem under Behavior, naming it; `not_configured`, `interrupted`
      and `missed` alone exit 0 (planned test_the_report_exits_one_on_each_problem).
- [ ] Every top-level command is in exactly one Postgres refusal class,
      `operator` included; `harrier operator` on SQLite refuses with the
      text under Behavior (spec 112's planned
      test_every_command_has_a_postgres_refusal_class, extended).
- [ ] `scheduler` and `schedule-report` each declare `harrier_scheduler`
      as the role they run as, and the parser walk reads the declaration
      (decision D2; spec 112's planned
      test_the_postgres_gate_has_operator_and_run_classes).
- [ ] `services/api/tests/test_schedule.py::test_rendered_plists_invoke_the_cli_without_a_shell`
      and the rest of `services/api/tests/test_schedule.py` pass
      unchanged, and `config/schedule.json` is unchanged.
- [ ] No file under `services/api/src/harrier/` other than
      `tracker/schema.py` names `owner_id`.

Documents

- [ ] ADR-013 decision 4 carries the "Amended (spec 108)" note.
- [ ] Once approved, the specs named under "Changes to approved specs"
      carry those lines and nothing else in them changes; spec 120 exists
      with `approved: no`.
- [ ] `docs/architecture.md`'s hosted section names the scheduler, its
      roles and this spec.
- [ ] CI's `check-python` job runs every new Postgres test against
      Postgres, none skipped.
- [ ] `just check` passes.

## Honest limitations

- **Spec 106's refined text does not yet carry what this spec needs.**
  Both were refined on 2026-10-10. This spec states what it needs from
  the run binding and asks it of 106 (Changes to approved specs). If 106
  settles differently, this spec is amended with it.
- **The scheduler's login can act as any owner.** It sets claims for
  whichever owner a slot belongs to, the same trust spec 106 states for
  the API's login. Row policy catches a bug in the scheduler; it does not
  contain a compromised scheduler.
- **Unconfirmed on a real project; each is a staging check for spec 110:**
  - that Supabase's `postgres` owns the function and has `BYPASSRLS`, so
    the function sees every owner (spec 105, Amendment, names the same
    assumption for `schema_version`);
  - that the migrating role may create `harrier_schedule_reader` and grant
    it, as for `harrier_tenant`;
  - superseded 2026-10-11 by the hosting budget, since no such group
    exists: that a Fly machine in a process group with no service is
    never stopped by Fly's proxy. The scheduled machine's assumptions
    replace it (Hosting budget, "Assumptions for spec 115");
  - that the API image's C library honors `TZ` for every zone `zoneinfo`
    accepts. The image is `python:3.12-slim-bookworm` (`Dockerfile:34`)
    and installs no `tzdata` package explicitly (`Dockerfile:97-98`,
    `:136-137`).
- **No retry.** A failed, crashed, timed-out or interrupted slot waits for
  the next slot. For discovery that is hours; for the academic job, a
  week.
- **Run output is not kept.** An owner or operator who wants to know why
  a run failed reads the outcome class and exit status, then reruns from
  the browser (spec 106) to see output.
- **No paging of the operator.** The report must be run, or spec 110 must
  wire it to an alert. Telegram stays the owners' channel (product
  invariant "Nothing auto-sends").
- **One listing for every owner, each tick.** Fine for an invite-only
  service (spec 102 open decision 5). Not measured at any size.
- **The shared cadence starts many owners at once.** All owners in one
  timezone are due at 09:00. The concurrency limit queues them; nothing
  spreads them.
- **The owner sees nothing new yet.** Hosted `GET /ops/schedule` still
  reads the committed file and launchd's wording
  (`services/api/src/harrier_api/ops_routes.py:37-41`, `:257-296`), and
  the browser prints "On the host, `{host_command}` reports it"
  (`apps/web/src/pages/operations/ScheduleSection.tsx:71`). Spec 120
  owns that.
- The line numbers cited here are refreshed by the change that implements
  this spec, not before (decision D9, 2026-10-11).

## Migration

None for the local product. SQLite records the new version and changes
nothing; launchd, `harrier schedule` and `config/schedule.json` behave as
before.

A hosted store reaches the new version through `harrier store migrate`.
No owner has a `schedule` row until they write one, so the scheduler's
first ticks find no due work. Its deploy is spec 110's.

## Changes to approved specs

Each line below is a change this spec asks of an approved spec. None is
written into that spec here; each is an amendment Akin approves with this
spec.

- **Spec 104:** the start check of the API's login role refuses a member
  of `harrier_schedule_reader` (Scope item 9), with an Amendment note.
- **Spec 105:** its Grants said "spec 108's due-owner list" would grant
  `service_role` what it needs. This spec grants `service_role` nothing;
  the listing is `EXECUTE` on `harrier_schedule_inputs()` for
  `harrier_schedule_reader` (Behavior, "The service-side role"). Its
  catalog gains `schedule_state` as an owned table and
  `harrier_schedule_inputs` as a second declared definer function;
  `OWNED_TABLES` gains `schedule_state`, migration 10 keeps a frozen list
  of its own, and `HOSTED_ONLY_TABLES` is declared for the parity test
  (Scope items 2 and 3). Spec 116 asks the same freeze and declaration;
  whichever lands first adds them.
- **Spec 106:** the run binding takes a caller other than the API, the
  commands and `TZ` listed under Behavior, "What this spec needs from spec
  106's run binding", a typed refusal, and a handle to wait on and
  terminate.
- **Spec 110:** the lines under Behavior, "What spec 110 provisions", and
  the four staging checks under Honest limitations. Spec 110's refined
  text of 2026-10-10 does not carry them; its Out of scope gives "the
  hosted scheduler and its machines" to this spec. Written into spec
  110's Amendment (2026-10-11) (decision D4).
- **Spec 112:** the CLI's Postgres gate takes a class for `harrier
  operator`, which runs only on Postgres (Scope item 8), with an Amendment
  note, and its planned test_every_command_has_a_postgres_refusal_class
  covers it. Written into spec 112 as its Amendment (2026-10-11), with
  each subcommand declaring its role (decision D2).

## Options weighed

Where the clock lives:

- **Fly scheduled machines.** A machine can be created with a schedule of
  `hourly`, `daily`, `weekly` or `monthly`, started on a "fuzzy" cycle
  (Fly, `fly machine run`). Fly's own guide says there is no fine-grained
  control and that it is "Not for precise timing" (Fly, Task scheduling).
  The cadence is four fixed times a day and a Monday morning, in each
  owner's timezone. Rejected on 2026-10-10. Chosen on 2026-10-11 under
  the hosting budget, as the clock for one tick per hourly start rather
  than for each job: the due computation stays harrier's and each owner's
  timezone stays exact, and only the start of a slot moves later, by up
  to about an hour (Hosting budget (2026-10-11)).
- **Supercronic in a process group.** A crontab in the image, one copy
  only, as Fly's guide warns (Fly, Task scheduling). A crontab cannot hold
  per-owner timezones or choices, so it would only tick a harrier command
  every minute, which a loop does without the extra binary. Rejected.
- **Fly's Cron Manager.** A separate app starting a machine per scheduled
  job from a static file (Fly, Task scheduling). It needs a second app and
  still a fan-out to owners in harrier code. Rejected for now.
- **Supabase Cron calling an API endpoint.** pg_cron runs jobs in the
  database and can make HTTP requests through pg_net (Supabase, Cron).
  pg_net is asynchronous, starts a request only after its transaction
  commits, times out after 2000 ms by default and keeps responses 6 hours
  in an unlogged table (Supabase, pg_net). Calling the API would put the
  cross-owner listing, or a per-owner start, behind an HTTP route without
  a user's token, which ADR-013 decision 3 forbids for the service role,
  and would store a credential for that route in the database. Computing
  due owners inside pg_cron would move the calendar into SQL. Rejected.
- **An in-process scheduler in the API.** ADR-006 rejected it for a
  laptop that sleeps (`docs/adr/ADR-006-scheduling.md`, "In-app
  scheduler"); hosted, the machine is up. It is rejected here for another
  reason: the API's login would then hold the cross-owner read in the
  process that serves HTTP, and API machines may be several or
  auto-stopped (spec 106). Rejected.
- **A harrier loop in its own Fly process group.** One machine, its own
  login, the committed cadence, any timezone, outcomes in the store.
  Chosen on 2026-10-10. Superseded 2026-10-11 by the hosting budget: one
  always-on shared-cpu-1x 1GB machine in Amsterdam costs $6.95 a month
  (Fly, Pricing, read 2026-10-11; spec 110, "Region (2026-10-11)"), over
  the 5 USD target alone. The loop
  body survives as one tick per scheduled start.

How due owners are listed:

- **Supabase's `service_role` with grants.** Spec 105 left this door
  open. A table grant to a `BYPASSRLS` role reads every owner's whole
  table, and `user_config` holds searches and watchlists beside the
  schedule. Rejected.
- **A dedicated role with `EXECUTE` on one definer function.** The bypass
  is one reviewed SQL body returning schedule fields only. Chosen; it
  amends ADR-013 decision 4's wording, as spec 105 amended decision 3's.

Where the owner's choice lives:

- **A `user_config` kind.** No new table for the choice, the existing
  write path, routes and validation. Chosen.
- **Columns on `schedule_state` with a new route.** A contract change and
  a second writer for configuration. Not chosen.

How a run gets the owner's time:

- **`TZ` in the child's environment.** No change to discovery; the gate
  and every other local-time read follow the owner. Chosen.
- **A new `--scheduled-at` flag.** Discovery would take the slot instant
  and its zone, and the gate would read it. A change to the local CLI for
  a hosted need. Not chosen.

## Decisions taken (2026-10-11)

Akin took these in chat on 2026-10-11 ("go with your recommendations").
The text above says where each changed it.

- **D2, one operator group.** `harrier operator` is one command group, and
  each subcommand declares the database role it runs as. This spec's
  `scheduler` and `schedule-report` and spec 111's `invite` and `erase`
  are its subcommands. Spec 112's gate gains exactly two classes beyond
  `harrier store`: operator, and spec 106's run class (Scope item 8,
  Behavior, "The CLI on each store").
- **D4, spec 110 takes this spec's asks.** "What spec 110 provisions" and
  the four staging checks under Honest limitations are written into spec
  110's Amendment (2026-10-11). Writing them found that Fly secrets are
  app wide, so a process group in the API app would receive the API's
  login; spec 110's Open decision 9 asks where the scheduler runs, and
  its answer may amend "Where the scheduler runs" here.
- **D9, line numbers.** Refreshed by this spec's implementing change
  (Honest limitations).

None of Open decisions 1 to 7 below was the subject of these decisions;
they stand as written, except item 1, superseded the same day by the
hosting budget, which adds item 8 (Hosting budget (2026-10-11)). Spec
110's Open decision 9, which D4 names, is superseded with it; spec 110's
Open decision 10 asks which app holds the scheduled machine.

## Open decisions for Akin

1. **Where the scheduler runs.** Recommended: a harrier loop,
   `harrier operator scheduler`, in its own Fly process group with one
   machine and a singleton advisory lock. Alternative: Supabase Cron with
   pg_net calling a scheduler endpoint, which keeps the clock in the
   database but puts service-side work behind HTTP.
   Superseded 2026-10-11 by the hosting budget. Open decision 8 asks it
   again.
2. **The service-side role.** Recommended: `harrier_schedule_reader`
   with `EXECUTE` on one definer function, and ADR-013 decision 4 amended
   to say so. Alternative: Supabase's `service_role` with `SELECT` grants
   on `schedule_state` and `user_config`, as spec 105 anticipated, which
   reads every owner's configuration.
3. **Hosted jobs in this spec.** Recommended: `discovery` and
   `academic-discovery`; `digest` joins with spec 109, `gmail-watch` with
   a hosted Gmail spec. Alternative: offer `digest` now, recorded
   `not_configured` for every owner until spec 109.
4. **Per-owner cadence.** Recommended: one committed cadence; the owner
   chooses jobs and a timezone. Alternative: per-owner times in the
   `schedule` kind, which needs bounds on how often an owner may schedule
   a paid source.
5. **A per-owner money ceiling.** Recommended: none in this spec; the
   bounds are one start per slot, no catch-up, the existing per-run
   ceilings, and the owner's own Apify account limit. Alternative: a
   per-owner weekly budget, which needs harrier to price every Apify run,
   and the LinkedIn source does not price its runs today.
6. **Run output.** Recommended: read and discarded; the outcome class and
   exit status are the record. Alternative: write it under the owner's
   storage prefix through spec 107's seam, scrubbed, which makes 107 a
   dependency again and adds retention.
7. **Delivery.** Recommended: this one spec, two pull requests, store
   first. Alternative: two specs, the store and the process, with the
   process depending on the store.
8. **What starts a tick.** Added 2026-10-11 by the hosting budget.
   Taken 2026-10-11 (Akin, "go with your recommendations"): the recommended answer.
   Recommended: a Fly scheduled machine on the `hourly` cycle, restart
   policy `no`, running one tick per start and exiting, billed only while
   it runs plus its root file system while stopped. Alternatives: a plain
   stopped machine started each hour by a GitHub Actions `schedule`
   workflow with `fly machine start`, which needs a Fly token in GitHub
   that can start the machine and whose own timing was not read here; or
   Supabase Cron with pg_net waking the API, rejected under Options
   weighed for putting service-side work behind HTTP.

## Proof / origin

- `docs/adr/ADR-013-hosted-multi-tenant-deployment.md`, decisions 2, 3, 4
  and 8, with spec 105's amendments.
- `docs/adr/ADR-006-scheduling.md`: launchd, the cadence in one file, the
  in-app option and why it was rejected for a laptop.
- `specs/020-scheduling-install.md`: the rendered plists.
- `specs/029-run-outcome-visibility.md`: exit status, last success, the
  overdue rule.
- `specs/097-an-academic-track-finds-positions-on-its-own.md`: per-run
  ceilings and the academic schedule.
- `specs/105-every-personal-table-is-owned-and-policed.md`: Grants (the
  service role and "spec 108's due-owner list"), the catalog, and the
  Amendment (`harrier_tenant`, a bypassing migrator).
- `specs/104-the-hosted-api-authenticates-every-request.md`: the request
  transaction's shape and the login-role checks.
- `specs/112-the-tracker-domain-runs-on-either-store.md`: the connection
  type, its `dialect`, update-then-insert, and the CLI's Postgres classes.
- `config/schedule.json`, `services/api/src/harrier/schedule.py`,
  `services/api/src/harrier/discovery.py`,
  `services/api/src/harrier/runoutcome.py`,
  `services/api/src/harrier/tracker/schema.py`,
  `services/api/src/harrier_cli/main.py`,
  `services/api/src/harrier_api/runs.py`: every line cited above.
- Fly, `fly machine run`, the `--schedule` flag ("fuzzy" cycle; scheduled
  machines cannot be started by command):
  https://docs.fly.io/machines/flyctl/fly-machine-run, read 2026-10-10.
- Fly, Task scheduling (scheduled machines "Not for precise timing";
  Supercronic, run one copy; Cron Manager):
  https://docs.fly.io/blueprints/task-scheduling, read 2026-10-10.
- Fly, Process groups (one image, its own machines per group, `fly scale
  count` per group, services choose their processes):
  https://docs.fly.io/launch/processes, read 2026-10-10.
- Fly, Autostop/autostart (autostop is part of each service):
  https://docs.fly.io/launch/autostop-autostart, read 2026-10-10.
- Supabase, Cron (jobs from every second to once a year; SQL, functions
  or HTTP requests; no more than 8 concurrent jobs and 10 minutes each
  recommended; run history in `cron.job_run_details`):
  https://supabase.com/docs/guides/cron, read 2026-10-10.
- Supabase, Cron quickstart (`net.http_post` example; run details not
  cleaned up automatically): https://supabase.com/docs/guides/cron/quickstart,
  read 2026-10-10.
- Supabase, pg_net (asynchronous, after commit, 2000 ms default timeout,
  responses kept 6 hours in an unlogged table, about 200 requests per
  second): https://supabase.com/docs/guides/database/extensions/pg_net,
  read 2026-10-10.
- Supabase, Connecting to Postgres (transaction mode does not support
  session advisory locks; a direct connection or session mode for a
  long-lived server): https://supabase.com/docs/guides/database/connecting-to-postgres,
  read 2026-10-10.
- PostgreSQL, Explicit Locking (a session-level advisory lock is held
  until released or the session ends):
  https://www.postgresql.org/docs/current/explicit-locking.html, read
  2026-10-10.
- PostgreSQL, Advisory Lock Functions (`pg_try_advisory_lock` returns
  false without waiting):
  https://www.postgresql.org/docs/current/functions-admin.html, read
  2026-10-10.
- Apify, Billing (an account's platform usage limits can be adjusted; at
  the limit platform services are suspended):
  https://docs.apify.com/platform/console/billing, read 2026-10-10.
- Apify JS client, `ActorStartOptions` (`maxTotalChargeUsd` caps a
  pay-per-event run's charge):
  https://docs.apify.com/api/client/js/reference/interface/ActorStartOptions,
  read 2026-10-10.

## Out of scope

- The local schedule: launchd, `harrier schedule`, `config/schedule.json`
  and every local behavior stay as they are.
- The owner's half, split into spec 120, "The owner sees and sets their
  schedule": a browser form for the `schedule` kind; hosted `GET
  /ops/schedule` listing the owner's chosen jobs, their last outcome and
  wording that names the hosted scheduler instead of launchd.
- The run binding itself, credentials and the child's environment: spec
  106.
- Telegram per owner and the hosted digest: spec 109. Gmail watch hosted:
  a later spec.
- Deploying the scheduler, its login and its limits: spec 110.
- Deleting an owner's `schedule_state`: spec 111.
- Retries, per-owner cadences, spreading owners across a slot, and an
  operator alert.
- Found while drafting, each for its own spec:
  - The LinkedIn source passes no `maxTotalChargeUsd` and no `maxItems`
    when it starts a run (`services/api/src/harrier/sources/apify_linkedin.py:59-66`,
    `:103-130`); only the academic source and contact discovery pass a
    limit (`apify_academic.py:173-178`,
    `services/api/src/harrier/outreach/discovery.py:150`), counted with
    `grep` for `maxTotalChargeUsd` and `maxItems` under
    `services/api/src`. Its spend is bounded by `count` alone. Spec 009
    owns that source.
  - The number of academic tracks per owner is not bounded, so the
    academic job's weekly cost is the number of tracks times each
    track's ceiling. Spec 097 owns the ceilings.

## Hosting budget (2026-10-11)

Akin decided in chat on 2026-10-11 that the hosted deployment targets
under 5 USD per month, with ADR-013's topology kept: the API and runs on
Fly, Vercel serving only the SPA. Spec 110's "Hosting budget
(2026-10-11)" holds the prices and the arithmetic. One always-on
shared-cpu-1x 1GB machine in Amsterdam costs $6.95 a month (Fly, Pricing,
read 2026-10-11: 6.70 × 1.038461538 = 6.96, shown by the page's region
selector as $6.95), so an always-on scheduler machine would break the
budget before the API ran at all. The long-lived loop in its own process
group (Open decision 1) is superseded. This section replaces it, and the
text above says where.

Region: Fly `ams` (Amsterdam) and Supabase `eu-west-1` (Ireland) since
2026-10-11, replacing Frankfurt; spec 110's "Region (2026-10-11)" holds
the decision.

The scheduler keeps its narrow role: it reads schedules only through
`harrier_schedule_inputs()` as `harrier_schedule_reader`, and every other
statement it runs is in a transaction as one owner. The bypass still
carries no domain code: the definer function returns schedule fields
only, and nothing else runs past the owner policy. None of that changes.

### What starts a tick

A Fly scheduled machine (Open decision 8):

- Created once per environment with `fly machine run <image> --schedule
  hourly --restart no`, running `harrier operator scheduler`, in the app
  spec 110's Open decision 10 names, with no service.
- Fly's `--schedule` "starts the Machine on a fuzzy `hourly`, `daily`,
  `weekly`, or `monthly` cycle", running "once per (approximate)" interval.
  Scheduled Machines "cannot be started via flyctl or Machines API
  commands", and suit Machines "that do a finite job, then exit" (Fly,
  `fly machine run`, read 2026-10-11).
- The default restart policy of `fly machine run` is `always`, which
  would restart the tick as soon as it exits; `--rm` destroys the Machine
  when it stops, which would end the schedule. Restart policy `no` is
  chosen.
- A running Machine is billed at its rate; a stopped one only for its
  root file system, at $0.15/GB a month (Fly, Pricing, read 2026-10-11).

### What one start does

One tick, as "A tick" under Behavior states it: take the lock or stand
by and exit 0; mark rows left `running` as `interrupted`; list; compute;
queue; start up to the concurrency limit; wait for each run to finish,
time out or be refused; record each outcome; exit 0. A refused start
check or a lost lock exits 1, and the next scheduled start is the retry.

### Timing

- Slots stay exact instants in each owner's timezone (The slot). A slot
  is started by the first tick at or after it, so it starts late by the
  time to the next hourly start. Fly calls the cycle approximate and
  gives no bound; spec 115's F3 notes the spread it observes.
- The Apify gate opens on weekday mornings before 12:00 local
  (`discovery.py:130-133`), three hours after the 09:00 discovery slot
  (`config/schedule.json:11-14`). A start delayed by about an hour keeps
  the gate open; a long queue on top of it may not (Failure modes, "A
  queue longer than the morning").
- No catch-up and one start per slot are unchanged: the claim, not the
  clock, decides.
- A tick that runs past the next hourly start holds the lock. Whether Fly
  skips that start or delays it is not stated; either way a second
  holder cannot start runs (One scheduler at a time). Spec 115's F3
  checks it.

### Staging

Staging's scheduler machine is created without `--schedule` and started
by hand for a check, so it costs its root file system between checks
(spec 110, "Hosting budget (2026-10-11)", Staging). The hourly cycle
itself is probed by spec 115's F3.

### Cost

**Estimate:** an hourly cycle starts the machine at most 720 times in a
30-day month (24 × 30). Its cost is the sum of those ticks' running
times at the machine's rate, about 0.0097 USD an hour for 1GB in
Amsterdam (6.95 / 720 = 0.00965, spec 110), plus its root file system while
stopped. A tick with nothing due has not been timed, so no monthly total
is given. Spec 110's monthly cost check sees this line.

### Assumptions for spec 115

Spec 115's F3, recorded confirmed or refuted there:

- the scheduled machine fires about hourly, and the observed spread of
  its start times is recorded;
- with restart policy `no` it stays stopped after exit until its next
  scheduled start;
- it is not started a second time while a tick still runs;
- `fly machine update` to a new image keeps its schedule.

### Acceptance criteria (hosting budget)

- [ ] One start of `harrier operator scheduler`, with due slots for two
      owners and a fake binding, starts both runs, waits for both,
      records both outcomes and exits 0; with nothing due it lists once
      and exits 0 (planned test_one_scheduler_start_runs_one_tick_and_exits).
- [ ] With the lock held by another session, a start lists nothing,
      starts nothing and exits 0 (planned test_a_second_scheduler_stands_by,
      extended).
- [ ] `schedule-report` with no tick running and no slot unclaimed prints
      `scheduler: no tick running` and exits 0 (planned
      test_the_report_exits_one_on_each_problem, extended).
- [ ] Spec 115's F3 is recorded confirmed or refuted before production
      schedules any owner, and a refuted item amends this section first.

### Honest limitations (hosting budget)

- A slot starts late by up to about an hour, by an amount Fly does not
  bound. Not measured here.
- If Fly stops firing the schedule, nothing alerts. `schedule-report`
  shows it as an unclaimed slot when the operator runs it.
- The report no longer treats an unheld lock as a problem, so a
  scheduler that never starts is seen only through unclaimed slots, one
  longest gap later.

### Proof / origin (hosting budget)

- Fly, `fly machine run`: `--schedule` fuzzy cycles, "once per
  (approximate)" interval, scheduled Machines not startable by command,
  for "a finite job, then exit", restart default `always` unless `--rm`,
  which destroys the Machine when it stops. Read 2026-10-11.
  https://docs.fly.io/machines/flyctl/fly-machine-run
- Fly, Task scheduling: scheduled Machines are "Not for precise timing";
  Cron Manager needs a separate app. Read 2026-10-11.
  https://docs.fly.io/blueprints/task-scheduling
- Fly, Pricing: shared-cpu-1x 1GB in the base region $6.70 a month for
  30 days of continuous runtime, times Amsterdam's region multiplier
  1.038461538, shown as $6.95; a stopped Machine billed only for its root file
  system at $0.15/GB a month. Read 2026-10-11.
  https://docs.fly.io/about/pricing

## Amendment (2026-10-12): the scheduler machine after an update

Spec 115's F3 ran a scheduled probe machine from 2026-10-11 to
2026-10-12 (spec 115, Amendment (staging findings), "The Fly checks
(2026-10-12)"). It confirmed the four items under "Assumptions for spec
115" with one condition, and it answered the question "Timing" left
open. This section turns both into behavior. Where the text above
disagrees, this section holds. It adds scope to an approved spec, so it
gates nothing until Akin agrees to it.

### What F3 showed

- A plain `fly machine update` to another image tag keeps the schedule:
  the next start came on the machine's hourly offset.
- `fly machine update --skip-start` keeps `"schedule": "hourly"` in the
  machine's config, but the machine fires no tick until it is started
  once. On the probe, the 10:19 and 11:19 starts did not happen. One
  `fly machine start` started it, although Fly's page says a scheduled
  Machine cannot be started by command (Proof / origin (hosting
  budget)), and after that tick the schedule resumed on its original
  offset.
- An update made while a tick runs stops that tick: SIGINT, then SIGTERM
  a second later.
- A start that falls while a tick still runs is skipped, not queued. The
  probe's 70 minute tick, 11:21:15Z to 12:31:15Z, covered the 12:19
  start; nothing started then or when the tick ended, and the next start
  was 13:19.

### The deploy

Each deploy moves the scheduler machine to the API's image digest with
`fly machine update` (spec 110, Amendment (2026-10-11), "The
scheduler"). From this amendment:

- **The update never passes `--skip-start`.** A deploy that must pass it
  runs `fly machine start` on the same machine right after the update,
  and that start runs one tick.
- **An update stops a running tick.** Its runs are terminated with it and
  their rows stay `running`. The next start marks them `interrupted`
  before it lists, as "What one start does" already says. Nothing new is
  needed for this; it is stated so a deploy is known to cost the tick in
  flight.
- **A check after the update.** The production deploy job, after its
  last step, reads the scheduler machine's events (`fly machine status`)
  until one is a `start` from source `flyd` later than the update, for at
  most 70 minutes: the next hourly start is at most 60 minutes away,
  plus a margin. It then prints `scheduler ticked after the update` and
  exits 0. When no such start appears in that time, it exits 1 naming
  the machine id, and the operator starts the machine by hand. Staging's
  scheduler machine has no schedule (Staging), so the check runs on
  production only.

### Timing, answered

"Timing" said Fly's behavior for a start that falls while a tick runs
was not stated. It is skipped, not queued. A tick that runs past the next
hourly start therefore drops that start, and a slot that came due in the
dropped hour is listed by the start after it. With no catch-up, only the
latest slot of each job is started then (The slot). Such a slot starts
up to about two hours after its time instead of one.
`HARRIER_SCHEDULED_RUN_TIMEOUT_SECONDS` bounds how long a tick can run,
and so how often this happens.

### Changes to approved specs (this amendment)

- **Spec 110:** its deploy carries the update rule and the check above,
  under its Amendment (2026-10-11), "The scheduler", and its `services/api/tests/test_deploy_files.py::test_the_deploy_workflow_migrates_before_it_deploys`. Not written into
  spec 110 here; Akin approves it with this amendment.

### Acceptance criteria (the scheduler machine after an update)

- [ ] The deploy's `fly machine update` of the scheduler machine carries
      no `--skip-start`, or is followed by `fly machine start` of the
      same machine before the next step (spec 110's `services/api/tests/test_deploy_files.py::test_the_deploy_workflow_migrates_before_it_deploys`, extended).
- [ ] The check after the update passes on an event list holding a
      `start` from `flyd` later than the update, and exits 1 naming the
      machine id on one holding only the update, or a start from before
      it, once 70 minutes have passed, against a fake event list and a
      fake clock (`services/api/tests/test_deploy_checks.py::test_the_scheduler_ticks_after_a_deploy`).
- [ ] The first production deploy that updates the scheduler machine
      records the check's output in its pull request, with no token,
      organization id or private hostname.

### Honest limitations (the scheduler machine after an update)

- F3 watched one probe machine for about 21 hours. The one overlap, at
  12:19, fell inside the tick started by hand after the `--skip-start`
  update, so on its own it does not separate "skipped while a tick runs"
  from that update's effect.
- `fly machine start` on a scheduled machine worked on the probe against
  what Fly's page says. The deploy relies on it only on the
  `--skip-start` path; if Fly enforces its page, the plain update is the
  path left.
- The check holds the production deploy job open for up to 70 minutes.
- A plain update of a machine with no schedule, staging's, was not
  probed. Whether it starts that machine is for spec 110's first staging
  deploy to see.

## Amendment (2026-10-13): what building the store settled

Building PR 1, the store (Scope items 1 to 4), found three places where
the text above was silent or wrong, and settled what it left open. Each is
stated here so the code and the spec agree. Where the text above
disagrees, this section holds. PR 2 is not built: spec 106's run binding,
which it starts runs through, is not on `main`.

### The migration's version

Migration 17, the next free version on `main` at `4b5c48d`
(`services/api/src/harrier/tracker/schema.py`, `POSTGRES_SCHEDULE_STATE`).
`MIGRATIONS` holds 17 with no statements and `SINGLE_DIALECT_MIGRATIONS`
declares it Postgres only. Specs 106 and 116 each add a Postgres-only
migration too. Spec 116's (PR #310) is 18, and the Postgres runner skips
any version at or below the highest it has recorded, so this change
merges before that one: a store that reached 18 first would never apply
17.

### Where the text was wrong

- **The owner is read through `harrier_owner_id()`, not `auth.uid()`.**
  The DDL under "The store" gives `owner_id` the default `auth.uid()`, and
  the policy "that every owned table has". Since migration 16 (spec 105,
  amendment of 2026-10-11) every owned table defaults `owner_id` to
  `public.harrier_owner_id()` and its `owner_only` policy compares with
  it, because Supabase's `postgres` cannot grant the tenant role anything
  in schema `auth`. `schedule_state` does the same. With `auth.uid()` the
  catalog test names the default as depending on it, and a tenant insert
  would be refused on Supabase
  (`services/api/tests/test_owner_policy.py::test_every_table_is_owned_or_declared`).
- **`HOSTED_ONLY_COLUMNS` covers the tables both stores have.** It was
  built from every owned table, so `schedule_state` would have been
  declared twice: as a hosted-only table and as a table with a hosted-only
  column, which the parity test reads as a stale column declaration.
  It now leaves out the tables `HOSTED_ONLY_TABLES` declares.
- **`HOSTED_JOBS` lands with the store.** Scope item 5 puts the hosted
  job set in `services/api/src/harrier/hostedschedule.py`, a PR 2 module,
  but the `schedule` kind (item 4, PR 1) refuses a job outside it. PR 1
  creates the module holding `HOSTED_JOBS` alone; PR 2 adds the rest.

### Where the text was silent

- **Both fields are required.** "The owner's choice" lists the refusals
  but not a missing field. A schedule without a timezone has no slot, and
  one without `jobs` is not a choice, so each is refused naming the field:
  `schedule.timezone is required`, `schedule.jobs is required`. A
  `timezone` that is not text, and a `jobs` that is not a list, are
  refused the same way.
- **A refusal quotes nothing the owner wrote.** Each message names
  `schedule`, `schedule.timezone` or `schedule.jobs`. An unknown field is
  counted, never named, since a key is the owner's text: `schedule has 2
  unknown fields; expected only ['jobs', 'timezone']`. The refusal chains
  no exception either: `zoneinfo`'s error quotes the zone, so the timezone
  refusal is raised outside the `except` block, and neither `__cause__`
  nor `__context__` carries it (`raise ... from None` would still leave
  it as `__context__`). The scheduler may then log a refusal as it is,
  traceback included, which "A tick" asks of it
  (`services/api/tests/test_hosted_schedule.py::test_the_schedule_kind_is_hosted_only`).
- **A key naming a directory of the tz database**, such as `Europe`, is
  refused as a zone `zoneinfo` cannot load.
- **The routes.** `GET /config` lists `schedule` hosted, after the local
  kinds, and `GET`, `PUT` and `DELETE /config/schedule` answer it; on
  SQLite each answers 404 as for any unknown kind. A kind with no row
  reads as today's other store-only kinds read: `source` `file`, `value`
  null. `harrier.userconfig.kinds_for` gives the kinds a connection holds,
  and the routes read it (`services/api/src/harrier_api/app.py`).
- **The schedule reader's catalog rules.** The catalog test also holds
  `harrier_schedule_reader` to the attributes the migration creates it
  with (no `SUPERUSER`, `BYPASSRLS`, `LOGIN` or `INHERIT`), to no
  membership, to no privilege on any table, column or sequence in
  `public`, and to `EXECUTE` on `harrier_schedule_inputs()` and on no
  other function there.

### Tests the criteria did not name

- `services/api/tests/test_hosted_schedule.py::test_the_routes_serve_the_kind_hosted`:
  the hosted app serves the kind through the request's connection, and a
  refused value is a 400 naming the field.
- `services/api/tests/test_hosted_schedule.py::test_the_local_routes_do_not_know_the_kind`:
  the local API answers 404 for the kind and lists the local kinds only.
- `services/api/tests/test_hosted_schedule.py::test_the_hosted_jobs_are_committed_jobs`:
  each hosted job is a job `config/schedule.json` names, and the set is
  Open decision 3's.
- `services/api/tests/test_owner_policy.py::test_a_failed_migration_leaves_no_trace_of_itself`
  runs for migration 17 too, and its fingerprint now reads the tenant and
  reader roles' attributes and memberships.
- `services/api/tests/test_hosted_schedule.py::test_schedule_state_has_the_specs_columns`:
  the table's columns, types and nullability are the DDL's, each outcome
  the spec lists is stored, any other is refused by the `CHECK` (23514),
  and a null slot by `NOT NULL` (23502).
- `services/api/tests/test_dialect_parity.py::test_a_hosted_only_table_out_of_place_fails_the_whole_comparison`:
  the comparison the parity test runs reports a declared table SQLite has
  too, and one Postgres lacks, on real stores.

### Existing tests that change

- `services/api/tests/test_owner_policy.py`: the owned-table tests take
  `schedule_state` with synthetic values; it has no `DELETE` grant, so
  owner A's delete of B's rows is refused before the policy, as on
  `tracks`; `test_track_two_needs_migration_16_on_the_faithful_shim`
  expects `target_version()` rather than 16 after a full migration; and
  an undeclared function executable by `PUBLIC` is now also named as
  executable by the reader.
- `services/api/tests/test_dialect_parity.py`: the owner defaults it
  reads are every owned table's, a hosted-only table's among them.
- `services/api/tests/test_domain_sql_on_both_stores.py::test_no_domain_insert_supplies_an_id`
  leaves out hosted-only tables: `schedule_state`'s writer is PR 2's
  scheduler, which runs on Postgres alone.

### Mutations

Each new behavior was removed or changed in the code, and the test that
proves it was run: twenty mutations, each failing its test, and
eighteen more for the local reviews' findings below. The list, each with
its result, is in the pull request.

### From the local reviews of PR #309

The privacy and data integrity reviews each found the two points above
in the first version: the unknown-field refusal named the owner's keys,
and the timezone refusal chained `zoneinfo`'s error, which holds the
zone. Both are fixed as stated, and the test fails with either put back.

The test integrity review found places where a wrong change still
passed, each confirmed by a mutation. Each test now fails under that
mutation:

- the parity comparison is tested on real stores, not only its helper;
- the listing compares every column of every row, as a sorted list, so a
  swapped, renamed, null or repeated column or row fails, and the
  owners' other kinds (a watchlist and discovery settings) stay out;
  `schedule_updated_at` is `user_config.updated_at`;
- `schedule_state`'s shape and refusals are pinned (above);
- the catalog's reader checks are each broken once: `BYPASSRLS`,
  `LOGIN`, `INHERIT`, a membership of `harrier_tenant`, and a privilege
  on a sequence. The role is created only when absent, so a test server
  that already has it would not see a wrong `CREATE ROLE`; these cases are
  what show the checks fire;
- the refusals are compared whole, and the `OSError` branch is reached by
  a `ZoneInfo` that raises `IsADirectoryError`, as some systems do for a
  directory of the tz database;
- `test_no_domain_insert_supplies_an_id` leaves hosted-only tables out on
  SQLite only. On Postgres it leaves out `schedule_state` by name, until
  PR 2's scheduler writes it.

Two points are recorded as they stand:

- **Definer functions are allowed by name, not signature.** The catalog
  declares `harrier_new_owner` and `harrier_schedule_inputs` by name, as
  it declared the first. An overload under either name with a body of its
  own would pass the definer rule; the `EXECUTE` rules still apply to it.
- **The rollback fingerprint reads harrier's two roles only.** Roles
  belong to the server and other tests make and drop their own meanwhile,
  so a failed migration 17 is checked to leave the tenant and reader
  roles as they were, and no other role.

Two points belong to PR 2 and are recorded here for it:

- **Two timestamp formats meet in the due rule.** `schedule_updated_at`
  is `user_config.updated_at`, written by `store_now()` as `YYYY-MM-DD
  HH:MM:SS`; `last_slot_at` is ISO 8601 with `T` and `Z`. Compared as
  text, a space sorts before `T`, so on one day the two order wrongly. PR
  2's "at or after the row's `updated_at`" parses both before comparing,
  and its planned test_a_new_choice_starts_at_the_next_slot covers a
  choice made on the slot's own day.
- **The zone database is the image's.** `tzdata` is a dependency on
  Windows only (`services/api/uv.lock`); the hosted image reads the
  system's zone files. An image without them refuses every schedule
  write at the timezone check. Spec 110's staging check on `TZ` covers
  the image; PR 2's due computation reads the same files.

### Honest limitations (the store)

- **The reader role is created only when absent,** as the text says and
  as migration 10 did for the tenant role. A `harrier_schedule_reader`
  made earlier with other attributes keeps them; the catalog test names
  each, but nothing corrects it on a live store. Migrations 13 and 14
  added that correction for the tenant role after review.
- **Staging is not checked here.** Spec 110's first two staging checks
  under Honest limitations (that Supabase's `postgres` owns the function
  with `BYPASSRLS`, and may create and grant the role) still stand.
- The line numbers cited above are not refreshed by this change; decision
  D9 leaves that to the change that implements the process.
