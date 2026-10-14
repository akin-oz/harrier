---
spec: 141
title: A local store imports into one hosted account
status: proposed
approved: no
milestone: M9
depends: [061, 075, 091, 103, 105, 106, 112, 137]
---

# Spec 141: A local store imports into one hosted account

## Problem

The hosted deployment (ADR-013) is live on staging, and an account there
starts empty: the sign-up trigger gives it track 1 and nothing else
(`harrier_new_owner()`, `services/api/src/harrier/tracker/schema.py`). The
operator's search so far lives in the local SQLite store, `data/tracker.db`:
its tracks, jobs, the decision history in `job_events`, eligibility
verdicts, contacts, profile documents and configuration documents.

No path moves it. Spec 111 covers export from a hosted account and deletion,
not import. The web app writes one row at a time through the API, so
re-entering the history by hand loses every past event and its timestamp.

Three properties of the hosted schema make a plain copy wrong:

- `jobs.id`, `job_events.id` and `job_eligibility.id` come from identity
  columns shared by every owner (`_PG_ID` in `schema.py`). A local id copied
  as written can collide with another owner's row, and it leaves the
  sequence behind the ids in use.
- Track ids are numbered per owner by `harrier_number_track()` on insert, and
  track 1 already exists for every account. A local track keeps its id only
  if the copy inserts in order and nothing else numbered a track first.
- `job_events` and `job_eligibility` are append-only, and tracks are never
  deleted, even by the table owner (spec 079, spec 105). A copy that stops
  halfway cannot be cleaned up by deleting what it wrote.

## Scope

1. A CLI command, `harrier store import-local --owner <user id>`, run on the
   operator's machine.
2. It reads the local store read-only and writes every row of the eight
   tables the store holds personal or configuration data in: `tracks`,
   `jobs`, `job_events`, `job_eligibility`, `contacts`, `profile_documents`,
   `user_config`, `job_runs`.
3. It writes as that owner through the connection seam and the owner's row
   policy, in one transaction.
4. It refuses an account that holds anything beyond the track sign-up made.

## Behavior

### Input

- `--owner` takes the account's Supabase user id, a UUID as the dashboard's
  Users table shows it. Not an email address: resolving an email needs a
  role that reads `auth.users`, and the import's login does not (Open
  decision 1).
- The hosted store is the URL in `HARRIER_IMPORT_DATABASE_URL`, exported in
  the operator's shell for the one command. Its login role must pass the same
  checks a run login passes (`services/api/src/harrier/loginrole.py`): it can
  log in, does not inherit, does not bypass row security, and reaches
  `harrier_tenant` and nothing past the policy. The run login,
  `harrier_run`, is the documented choice. The migrator's URL is refused.
- The local store is `data/tracker.db` under the data directory, as every
  host command finds it.

### Opening the local store

The import opens the local file the way a host command does: spec 061's
check that no container holds it, then spec 075's lease. It then reads it
with SQLite's read-only mode. It never opens it through
`harrier.db.connect()`, which migrates, so the import never writes to the
local file.

### What it checks before writing

In this order, each refusal naming what failed and writing nothing:

1. The local store's schema version equals the latest the code knows. A
   store behind it is refused with a sentence that says to start the local
   app once, so it migrates, and stop it again.
2. The hosted store's version equals the same latest version (spec 103's
   check).
3. The login role passes the run login's checks.
4. Inside the owner's transaction, after locking track 1's row
   (`SELECT ... FOR UPDATE`), the owner sees exactly one track, id 1,
   and no row in the other seven tables. An owner that sees no track is
   refused as an account that does not exist or has not been invited. An
   owner with rows is refused, naming the first table that holds any.
5. Every column of each local table has a hosted column of the same name,
   and every hosted column the copy does not fill has a default. A
   difference is refused, naming the table and the column, before any
   insert.

### What it writes

One owner transaction (`open_store(owner=...)`, spec 106), in this order:

1. Track 1 takes local track 1's values in every column but `id`.
2. Local tracks 2 and up are inserted in local id order. Each hosted id comes
   back from the insert, and a map from local to hosted track id is kept.
3. Jobs are inserted in local id order with `track_id` rewritten through the
   track map. Each new hosted id comes back, and a map from local to hosted
   job id is kept.
4. `job_events` and `job_eligibility` are inserted in local id order with
   `job_id` rewritten through the job map. Every other column, `at`
   included, is copied as written, so the history keeps its times and order.
5. `profile_documents` with `track_id` rewritten through the track map
   (null stays null). `contacts`, `user_config` and `job_runs` as written.
   `job_runs.job` names a pipeline job, not a tracker row, and is not
   rewritten.
6. Check 4 runs again just before commit, counting only rows this
   transaction did not write. If another writer added a row for the owner
   meanwhile, the transaction rolls back and the import is refused.

`owner_id` is never written by the import: the column's default reads the
owner from the transaction's claims (spec 105). A row therefore cannot land
in another account even if the code asked it to.

### Output

One line per table, `<table>: <number> copied`, then `imported`. Exit 0.
Nothing printed names the owner, a contact, a company or any document
content. A refusal prints one line to stderr and exits 1. A usage error
exits 2.

## Failure modes

| Case | Result |
|---|---|
| The local container is running | Refused by spec 061's check; nothing read, nothing written |
| `data/tracker.db` is missing | Refused, naming the path's role ("the local store"), exit 1 |
| Local store one version behind | Refused with the start-once sentence |
| `HARRIER_IMPORT_DATABASE_URL` unset or the migrator's | Refused before connecting to the owner |
| No such user id | Refused: no track visible to that owner |
| Account already imported, or used | Refused, naming the first non-empty table |
| Connection lost or any insert fails halfway | The transaction rolls back; the account is still empty; the import can run again |
| Two imports for one owner at once | The second blocks on track 1's row lock and then finds rows, so it is refused |
| The operator uses the web app during the import | A row it adds fails the pre-commit check; the import rolls back |
| A local column the hosted schema lacks | Refused before any insert, naming table and column |
| A timeout on the hosted connection | The transaction rolls back like any failure; the honest limit is named below |

## Acceptance criteria

- [ ] A local store with three tracks, jobs on tracks 1 and 3, events,
      eligibility verdicts, contacts, profile documents for a track and for
      none, and configuration documents imports into an empty account, and
      the owner reads back the same values in every copied column, with
      `job_id` and `track_id` pointing at the hosted rows
      (planned test_import_local_copies_every_table_and_rewrites_ids).
- [ ] Event and eligibility order and `at` survive the copy
      (planned test_import_local_keeps_history_times_and_order).
- [ ] A second owner's rows are untouched and invisible to the import, and
      no imported row carries another owner's id
      (planned test_import_local_writes_only_the_named_owner).
- [ ] An account with any row beyond track 1 is refused with exit 1 and no
      write (planned test_import_local_refuses_an_account_with_rows).
- [ ] A failure on the last table leaves the account as it was, and a second
      run then succeeds
      (planned test_import_local_rolls_back_whole_and_runs_again).
- [ ] A row added for the owner by another connection before commit rolls
      the import back
      (planned test_import_local_rolls_back_when_the_account_changes).
- [ ] A local store behind the latest version, a running container, the
      migrator's URL, and an unknown user id are each refused with exit 1
      and nothing written
      (planned test_import_local_refuses_before_writing).
- [ ] The local file's bytes and modification time are unchanged by an import
      (planned test_import_local_never_writes_the_local_store).
- [ ] Output names tables and numbers only
      (planned test_import_local_prints_no_identity).
- [ ] `docs/deploy.md` says how to run it against staging and production.

Tests run on the CI Postgres service, like spec 112's dual-store tests, with
synthetic rows only.

## Proof / origin

- Akin's decisions of 2026-10-14, after the first staging deploy: the import
  is an operator CLI, not a web upload; it refuses an account with data.
- The identity columns: `_PG_ID` and the per-owner keys in
  `services/api/src/harrier/tracker/schema.py`.
- Track numbering: `harrier_number_track()` in the same file.
- Append-only events and undeletable tracks: spec 079, spec 105, and spec
  111's Problem.
- The owner transaction and its login: `open_store(owner=...)` in
  `services/api/src/harrier/connection.py` (spec 106), the run login's checks
  in `services/api/src/harrier/loginrole.py` (spec 134).
- The host open rules: spec 061 (`services/api/src/harrier/db.py`) and spec
  075 (`services/api/src/harrier/hostlease.py`). The 2026-09-18 corruption,
  from a host write while the container ran, is why the import reads only.

## Open decisions for Akin

1. **`--owner` takes the user id, not the email.** Recommended. The email
   would need a role that reads `auth.users`, which is the operator login of
   spec 111 and is not built. The user id is on the dashboard's Users page.
2. **Artifacts wait for spec 107's build.** You chose rows and artifacts.
   Spec 107 is approved but nothing writes to the `harrier-files` bucket yet,
   and the hosted app cannot show an artifact until it does. Recommended:
   this spec moves rows now, and a follow-up spec moves `data/resumes`,
   `data/cover-letters` and `data/answers` once spec 107 is built. Their file
   names carry company and role, not job ids, so the new job ids do not
   break them. Also open there: whether `data/resumes-corrected` comes too.

## Out of scope

- Files of any kind (Open decision 2): artifacts, job descriptions,
  discovery caches, reports, logs, LLM logs, and the 2026-09-18 corrupt
  backup.
- Merging into an account that has rows, or a second import.
- Importing hosted data back to local (spec 111's export covers leaving).
- A web route or a Settings page for import.
- Credentials and Telegram settings: they are hosted only and entered in
  the web app (specs 109, 116).
- Changing the local store in any way.

## Honest limitations

- One transaction holds every row. At today's local size it is a few
  thousand inserts. A store large enough to outlast a connection or pooler
  timeout would need batching, which this spec does not do.
- The pre-commit check sees rows that committed before it. A write the web
  app commits after that check and before the import's commit is not seen;
  it cannot conflict with an imported row's id, so the account ends with
  both.
- Nothing proves the local and hosted columns mean the same thing, only that
  their names match. Spec 112's portable SQL is what keeps them aligned.

## Migration

None for existing data or users. The operator runs the command once per
account. Production gets the same command once its project exists.
