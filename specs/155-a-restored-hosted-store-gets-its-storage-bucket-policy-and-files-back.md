---
spec: 155
title: A restored hosted store gets its Storage bucket, policy and files back
status: accepted
approved: yes
milestone: M10
depends: [107, 110, 146, 153, 154]
---

# Spec 155: A restored hosted store gets its Storage bucket, policy and files back

## Problem

Spec 153 backs the hosted store up and documents a restore into a new, empty
Supabase project (`docs/deploy.md`, "Restoring the hosted store"). The
restore brings back the `public` schema and the rows of `auth.users` and
`auth.identities`. It does not bring back anything in `storage`, so after a
restore:

1. **The bucket `harrier-files` does not exist, and neither does the policy
   `harrier_files_owner_only` on `storage.objects`.** Migration 23 made both
   (`POSTGRES_STORAGE` in `services/api/src/harrier/tracker/schema.py`). The
   restored `schema_version` already reads 23 or later, so `harrier store
   migrate` treats migration 23 as applied and never runs it again. No other
   command makes them. Every file read and write by the hosted API and runs
   then fails, and nothing names the cause.
2. **`harrier store check-deploy` passes anyway.** `_check_storage_policies`
   in `services/api/src/harrier/storedeploy.py` refuses a policy other than
   harrier's, but it does not refuse a missing one, and it never looks at
   the bucket. The deploy runs it at every deploy (spec 110), so a restored
   project with no bucket and no policy deploys green.
3. **The copy `harrier store backup-files` makes cannot be uploaded.** It
   holds each object under its name below the owner's prefix (spec 153,
   "`backup-files` writes object names"). `import-artifacts` (spec 146)
   uploads from the local `data/` directories by key, not from that copy.
   Objects that exist only in the hosted store (files the hosted app wrote)
   have no way back.

Spec 153's Honest limitations record items 1 and 3 and assign them to a
follow-up spec. This is that spec. Who loses is the owner: a restore that
skips this leaves an account whose rows point at files nobody can read.

## Scope

1. `harrier store restore-storage`: makes the bucket and the policy in a
   restored store, as the migrator.
2. `harrier store check-deploy` refuses a store that lacks the bucket or the
   policy.
3. `harrier store restore-files --owner <user id> --from <dated directory>`:
   uploads a `backup-files` copy into the owner's prefix, create-only.
4. `docs/deploy.md`, "Restoring the hosted store": steps 6 and 8 name the
   two commands. Spec 153's Honest limitations point here.

## Behavior

### `harrier store restore-storage`

It runs as the migrator, with the store URL every `harrier store` deploy
command reads (`STORE_DEPLOY_COMMANDS` in
`services/api/src/harrier_cli/main.py`). It refuses a SQLite store. In one
transaction it:

1. Refuses a store whose schema version is below 23, saying that `harrier
   store migrate` makes the bucket and policy there.
2. Refuses a store without Supabase's storage objects, with migration 23's
   own message (`MIGRATION_23_NEEDS_STORAGE`).
3. Refuses a store that lacks `public.harrier_storage_user_active()`, which
   the policy calls. The `public` restore brings the function back, so its
   absence means the restore is incomplete. The command does not make it.
4. Refuses a bucket `harrier-files` with other settings, with migration 23's
   own message (`MIGRATION_23_OTHER_BUCKET`).
5. Makes the bucket if it is absent, with migration 23's settings (private,
   `MIGRATION_23_SIZE_LIMIT`, `MIGRATION_23_MIME_TYPES`).
6. Makes the policy if it is absent, with migration 23's text.

It prints two lines, `bucket harrier-files: made` or `already there`, and
`policy harrier_files_owner_only: made` or `already there`, then exits 0. On a
refusal it prints one line naming the step and exits 1, and nothing is made.
A second run prints `already there` twice and changes nothing. Migration 23's
statements stay as they are: the command reuses the bucket and policy
statements, not the migration.

### `harrier store check-deploy`

When `storage.objects` exists, check-deploy also refuses when the bucket
`harrier-files` is absent or the policy `harrier_files_owner_only` is
absent. The refusal names what is missing and the command that makes it:
`check-deploy: storage: bucket harrier-files is missing; run harrier store
restore-storage`. A store without `storage.objects` (a plain Postgres) is
reported as today: `storage.objects: absent, no policy to check`.

### `harrier store restore-files`

`harrier store restore-files --owner <user id> --from <dated directory>` runs
on the operator's machine. It reads `HARRIER_SUPABASE_URL` and
`HARRIER_SUPABASE_SECRET_KEY` from the shell, and refuses a secret key
defined in `.env` (`operator.read_admin`, as `backup-files` does). The
`--from` directory must be a dated directory under
`data/hosted/backups/files/`. Any other path is refused before any request.

For each file under the directory, the object's name below the owner's
prefix is its path relative to the directory. The upload goes to
`<owner>/<that name>`, without upsert (spec 154's create-only upload), with
the content type that the name's extension maps to in spec 107's
`CONTENT_TYPES`. Then:

| Storage answers | Counted as |
|---|---|
| Created | `uploaded` |
| Already exists, same bytes | `already there` |
| Already exists, other bytes | `left, differs` (never overwritten) |
| The extension maps to no content type | `not written` (no request sent) |
| Any other error after the 429 waits | `not written` |

It prints counts only, never a key or an object name. A file counted
`left, differs` is skipped and the run goes on: it does not change the exit
status. The command exits 0 when every file is `uploaded`, `already there` or
`left, differs`, and 1 when any file is `not written`, naming that count. A Storage 429 waits and asks again, as spec 146's import does
(`artifactimport.patiently`). A second run counts every file `already there`.

Restored `auth.users` rows keep their ids, so the owner's id and prefix are
the same in the new project as in the old one.

### Restore

In `docs/deploy.md`, "Restoring the hosted store":

- Step 6 runs `harrier store restore-storage` before `check-deploy`.
- Step 8 gives `restore-files --from` the newest dated directory, then
  `import-artifacts` for anything written locally after that copy.

The steps stay manual (spec 153, Out of scope).

## Failure modes

| Case | Result |
|---|---|
| `restore-storage` on the live staging store | `already there` twice, exit 0, nothing changes |
| `restore-storage` on a store below version 23 | Refused, naming `harrier store migrate`; exit 1 |
| The restore left out `harrier_storage_user_active()` | Refused, naming the function; exit 1; no bucket made |
| A bucket `harrier-files` with other settings | Refused with migration 23's message; exit 1; the bucket is not changed |
| A policy of another name on `storage.objects` | `restore-storage` does not touch it; `check-deploy` refuses it as today |
| `check-deploy` on a restored project before `restore-storage` | Refused, naming the missing bucket or policy and the command; the deploy stops |
| `restore-files` before `restore-storage` | Every upload fails because the bucket is missing; everything `not written`; exit 1 |
| `restore-files --from` outside the backups directory | Refused before any request; exit 1 |
| An object already in Storage with other bytes | Left, counted `left, differs`; the run goes on; exit 0 if nothing else failed |
| A 429 that outlasts the waits | Stops, naming the step, as spec 146's import does; exit 1 |
| Interrupted halfway | A rerun counts the uploaded files `already there` and continues |

## Acceptance criteria

- [x] On a Postgres store with the storage shim, at the latest version, with
      the bucket row and the policy dropped, `restore-storage` makes both,
      prints `made` twice and exits 0. A second run prints `already there`
      twice
      (`services/api/tests/test_store_deploy.py::test_restore_storage_makes_the_bucket_and_policy_a_restore_lacks`).
- [x] `restore-storage` refuses a store below version 23, a store without
      `harrier_storage_user_active()`, and a bucket with other settings. Each
      refusal exits 1 and makes nothing
      (`services/api/tests/test_store_deploy.py::test_restore_storage_refuses_an_incomplete_store_and_makes_nothing`).
- [x] `check-deploy` on a shimmed store without the bucket, and on one without
      the policy, raises `StoreDeployRefused` naming what is missing and
      `restore-storage`. On a complete store it passes as before
      (`services/api/tests/test_store_deploy.py::test_check_deploy_refuses_a_store_missing_the_bucket_or_policy`).
- [x] Against the fake Storage server, `restore-files` uploads every file of a
      dated directory without upsert, under `<owner>/<relative path>`, with
      its content type. It counts an equal object `already there`, leaves
      one with other bytes and goes on, exiting 0. It exits 1 when a file is
      `not written`. It prints no object name
      (`services/api/tests/test_restore_files.py::test_restore_files_uploads_a_backup_copy_create_only_and_names_none`).
- [x] `restore-files` refuses a `--from` outside
      `data/hosted/backups/files/` and a secret key defined in `.env`, both
      before any request
      (`services/api/tests/test_restore_files.py::test_restore_files_refuses_a_foreign_directory_and_an_env_key`).
- [x] `docs/deploy.md` names both commands at steps 6 and 8 of "Restoring the
      hosted store"
      (`services/api/tests/test_backup_workflow.py::test_the_runbook_states_the_restore_procedure`).
      Spec 153's Honest limitations point to this spec.

## Proof / origin

- Akin, 2026-10-17: "spec the storage bucket restore", after spec 153's
  build agent flagged the gap.
- The gap: spec 153, Honest limitations, and `docs/deploy.md`, "Restoring the
  hosted store", steps 6 and 8.
- The bucket and policy statements: migration 23, `POSTGRES_STORAGE` in
  `services/api/src/harrier/tracker/schema.py` (spec 107).
- The check that passes without them: `_check_storage_policies` in
  `services/api/src/harrier/storedeploy.py`.
- The create-only upload: spec 154. The operator binding and 429 waits:
  spec 107 (`SupabaseFileStore.for_operator`) and spec 146.

## Open decisions for Akin

1. **One spec or two.** Recommended: one. The bucket, the policy and the
   files are one restore, and the bucket is useless without its files.
   Alternative: split `restore-files` into its own spec, so the bucket and
   policy can land first.
2. **Whether check-deploy refuses a missing bucket or policy.** Recommended:
   yes, so a restored project cannot deploy green without them. Staging has
   both today, so its deploys are unchanged. Alternative: report only, and
   leave the procedure to catch it.
3. **An object already in Storage with other bytes.** Recommended: leave it
   and exit 1, because a new project should hold nothing yet, and overwriting
   would break spec 154's rule. Alternative: overwrite with the backup's
   bytes.

## Decisions taken (2026-10-17)

Akin approved the spec with these answers:

1. One spec: the bucket, the policy and the files land together.
2. `check-deploy` refuses a store that lacks the bucket or the policy.
3. An object already in Storage with other bytes is left (skipped), never
   overwritten, and it does not block the run: `restore-files` counts it
   `left, differs`, carries on, and does not exit 1 for it. This replaces
   the recommendation, which exited 1.

## Amendment (2026-10-17): the build

What the build settled that the text above left open. No behavior above
changes.

- `restore-storage`'s refusal lines, each after `error: `:
  `restore-storage: store version: the store is at version <n>, below 23;
  run harrier store migrate, which makes the bucket and the policy`;
  `restore-storage: storage: ` and `MIGRATION_23_NEEDS_STORAGE`;
  `restore-storage: function: public.harrier_storage_user_active() is
  missing, and the policy calls it; the restore of public is incomplete`;
  `restore-storage: bucket: ` and `MIGRATION_23_OTHER_BUCKET`. It picks
  migration 23's statements out of `POSTGRES_STORAGE` by their text
  (`migration_23_statement` in `services/api/src/harrier/storedeploy.py`),
  so `schema.py` is unchanged.
- `check-deploy` names both when both are missing: `check-deploy: storage:
  bucket harrier-files and policy harrier_files_owner_only are missing; run
  harrier store restore-storage`.
- `restore-files` prints `files: <count>`, the four counts in the table's
  order, then `restored`, or `incomplete: <count> not written` when it exits
  1. A 429 past the last wait stops with `upload: Storage answered 429 after
  every wait; run the command again to resume` (`compare:` when it came at
  the read of an existing object).
- `restore-files` never follows a symbolic link inside the directory: one is
  counted `not written`, with no request. `--from` is resolved, links
  included, before it is checked, so a link under the root that leads
  elsewhere is refused.
- The upload by object name is `SupabaseFileStore.create_object`, beside
  `read_object`, sharing spec 154's create-only request and answer.
- The settings page's terminal-only list (spec 096,
  `services/api/src/harrier_api/settings_routes.py`) names both commands, as
  it names every command.

## Out of scope

- Automating the restore, or running it from CI.
- Restoring roles, role settings or login passwords: the runbook's steps 2
  and 3 cover them by hand.
- Restoring other Supabase settings (auth settings, the project's keys).
- Changing migration 23, or making `harrier store migrate` re-run it.
- Running `restore-storage` in the nightly backup check: its Postgres
  service has no storage schema.
- Restoring another owner's files, or files into another owner's prefix.

## Honest limitations

- Nothing here runs on Supabase in CI. The tests use the storage shim and the
  fake Storage server. The first proof on Supabase is the operator's
  rehearsal on a scratch project, which `docs/deploy.md` already asks for.
- `restore-files` restores only as much as the newest `backup-files` copy
  holds. A hosted write after that copy is lost.
- A copy whose object name has an extension the bucket no longer accepts is
  counted `not written` and stays on disk.

## Migration

None. Both store commands are new, and `check-deploy`'s new refusal does not
fire on staging, which holds the bucket and the policy.
