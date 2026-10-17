---
spec: 153
title: The hosted store is backed up nightly, and each backup is restored before it counts
status: accepted
approved: yes
milestone: M10
depends: [030, 105, 107, 110, 111, 141, 146]
---

# Spec 153: The hosted store is backed up nightly, and each backup is restored before it counts

## Problem

The hosted account on staging now holds the operator's whole job search:
the rows spec 141 imported, the files spec 146 copied, and everything the
hosted API writes since. Staging is the real environment (Akin, 2026-10-17);
there is no separate production.

Nothing backs it up. Supabase's Free plan takes no automatic database
backups, and the deploy pipeline adds none (`docs/deploy.md`, "Backups").
Database backups would not include Storage objects either (spec 111,
"Backups"; spec 107, Honest limitations). A migration that goes wrong, an
operator command run against the wrong account, or a project lost to
Supabase's own trouble would take the only copy.

Spec 030 set the rule for the local store: a backup counts only once a
restore of it has been exercised. The hosted store has no backup to hold to
that rule.

## Scope

1. A scheduled GitHub Actions workflow, `.github/workflows/backup.yml`,
   that dumps the hosted database nightly, encrypts the dump, restores it
   into a throwaway database to prove it, and keeps it as a workflow
   artifact for 30 days.
2. An `age` recipient (a public key) committed to the repository, whose
   private key exists only in the operator's password manager.
3. A local command, `harrier store backup-files --owner <user id>`, that
   copies the account's Storage objects into a never-in-git directory on
   the operator's machine when the operator runs it.
4. The restore procedure in `docs/deploy.md`, including spec 111's rule
   that a restore re-erases every id the erasure ledger lists after the
   backup's time.
5. The workflow's logic as `harrier store` commands (`backup-dump`,
   `backup-check`, `backup-seal`), so it is tested in Python and not only read
   from YAML; and the PostgreSQL 17 client and `age` installed in the CI
   Python job (`.github/workflows/ci.yml`), so those tests run there. Added in
   the Amendment (2026-10-17) below.

## Behavior

### The nightly database backup

The workflow runs at 03:30 UTC every day on `main`, and on
`workflow_dispatch`. It runs in the `staging` GitHub environment and reads
only `HARRIER_MIGRATE_DATABASE_URL`, the migrator login the deploy already
holds there (spec 110). It never reads a Supabase API key.

1. **Dump.** `pg_dump` at the server's major version (17), custom format,
   of the `public` schema (every harrier table, its policies and
   functions, `schema_version`) and the rows of `auth.users` and
   `auth.identities`, which every owned row references. Nothing else from
   `auth` or `storage` is dumped.
2. **Manifest.** Beside the dump, a manifest records the time the dump
   started, the store's schema version, and each dumped table's row count.
   The manifest names no owner and no value.
3. **Restore check.** The job holds no private key, so the check runs on
   the plain dump, inside the job, before anything is encrypted. It
   restores the dump into an empty Postgres 17 service container and
   compares each table's row count and the schema version with the
   manifest. Any difference fails the job, and nothing is uploaded.
4. **Encrypt.** Dump and manifest are encrypted together with `age` to the
   recipient in `deploy/backup-recipient.txt`. Nothing unencrypted leaves
   the job: no unencrypted file is uploaded, cached or printed, and the job
   log names counts and the schema version only.
5. **Keep.** The encrypted file is uploaded as an artifact named
   `hosted-backup-<UTC date>` with a retention of 30 days. A failed run
   uploads no artifact, and GitHub's own failed-run notice to the
   repository owner is the alert.

The repository is public, so anyone signed in to GitHub can download the
artifact. Only the `age` private key decrypts it, and that key is never in
GitHub, Fly, Vercel or the repository.

### Files, on demand

`harrier store backup-files --owner <user id>` runs on the operator's
machine with `HARRIER_SUPABASE_URL` and `HARRIER_SUPABASE_SECRET_KEY` from
the shell, refused from `.env` as spec 111's operator commands refuse it. It
lists every object under the owner's prefix through spec 107's operator
binding (`SupabaseFileStore.for_operator`) and writes each to
`data/hosted/backups/files/<UTC date>/<key>`, under `data/`, which is
never-in-git. An object whose bytes equal the newest earlier copy of the
same key is linked from that copy rather than downloaded again. It prints
counts only, never a key or a name, and exits 0 when every object was
written, 1 naming the step otherwise. A Storage 429 waits and asks again, as
spec 146's import does.

### Restore

`docs/deploy.md` gains "Restoring the hosted store". In order: download the
artifact; decrypt it with the private key from the password manager;
stop the API (`fly scale count 0`); restore into the project as the
migrator; re-run `harrier operator erase` for every id the local erasure
ledger records after the manifest's time (spec 111); run `harrier store
check-deploy`; start the API. A file restore copies objects back with
spec 146's `import-artifacts` path or a sibling of it.

## Failure modes

| Case | Result |
|---|---|
| The migrator URL is unset or refused | The job fails before dumping; no artifact |
| `pg_dump` fails or is cut off | The job fails; no artifact |
| The restore check's counts or version differ from the manifest | The job fails and uploads nothing; the run's log names the table, not a value |
| The recipient file is missing or malformed | The job fails before uploading |
| A run is skipped (GitHub pauses schedules on a repository with no activity for 60 days) | No artifact that night; the 30-day window shrinks, and nothing says so but the artifact list |
| Supabase is unreachable | The job fails; the next night tries again |
| `backup-files` meets a 429 | It waits and asks again before stopping, as spec 146 does |
| The private key is lost | Every backup is unreadable; the honest limit below |

## Acceptance criteria

- [ ] The workflow dumps, checks and encrypts in that order (check before
      encryption), and a restore
      check that finds a count differing from the manifest fails the job
      with nothing uploaded (the failing check itself:
      `services/api/tests/test_hosted_backup.py::test_a_restore_check_that_finds_a_difference_fails_and_seals_nothing`)
      (`services/api/tests/test_backup_workflow.py::test_the_backup_workflow_uploads_only_after_the_restore_check`).
- [ ] No step of the workflow reads a Supabase API key, and only the dump
      step reads the migrator URL
      (`services/api/tests/test_backup_workflow.py::test_the_backup_workflow_reads_only_the_migrator_url`).
- [ ] The uploaded artifact is the `age` output alone, with a retention of
      30 days (`services/api/tests/test_backup_workflow.py::test_the_backup_artifact_is_encrypted_and_kept_thirty_days`).
- [ ] The dump command, run against the CI Postgres service with the shim
      and synthetic owners, restores into an empty database with every
      table's count and the schema version equal to the manifest
      (`services/api/tests/test_hosted_backup.py::test_a_hosted_dump_restores_with_its_manifest_counts`).
- [ ] `backup-files` writes each listed object under the dated directory,
      links an unchanged object from the newest earlier copy, prints no key,
      and refuses a secret key defined in `.env`
      (`services/api/tests/test_backup_files.py::test_backup_files_copies_every_object_and_names_none`).
- [ ] `docs/deploy.md` states the restore procedure, including the
      ledger re-erase, and names the workflow and the command.

## Proof / origin

- Akin, 2026-10-17: no separate production; staging holds the real data;
  back it up nightly in GitHub Actions, encrypted, kept 30 days; files on
  demand on his machine, so the Supabase secret key never enters GitHub.
- No backup today: `docs/deploy.md`, "Backups".
- The restore rule: spec 030. The ledger re-erase: spec 111, "Backups".
- The migrator secret in the `staging` environment: spec 110,
  `.github/workflows/deploy.yml`.
- The operator binding and the 429 handling: spec 107
  (`services/api/src/harrier/filestore.py`) and spec 146's amendment of
  2026-10-16.

## Open decisions for Akin

1. **What is dumped.** Recommended: the `public` schema plus the rows of
   `auth.users` and `auth.identities`, which a restore into a fresh project
   needs for every owned row's foreign key. Alternative: the whole database,
   which also carries Supabase's own schemas and is harder to restore into
   another project.
2. **Where the restore check runs.** Recommended: in the same job, on the
   plain dump, before encryption, so the job never needs the private key.
   Alternative: a separate weekly job that decrypts with a key held in
   GitHub, which puts the private key in GitHub.

## Decisions taken (2026-10-17)

Akin approved the spec with both open decisions as recommended: the dump is
the `public` schema plus the rows of `auth.users` and `auth.identities`, and
the restore check runs on the plain dump inside the job, before encryption.

## Amendment (2026-10-17)

Building showed these parts of the text above to be incomplete. Each is stated
here, in the change that built them.

- **Two dumps, one encrypted file.** `pg_dump` with `-t` dumps tables only: a
  dump of `public` plus the two `auth` tables in one archive loses every
  function the triggers call. So the dump step runs `pg_dump` twice at one
  exported snapshot, `public.dump` (the `public` schema) and `auth.dump` (the
  two tables, with the new-owner trigger that belongs to `auth.users`). The
  encrypted file is a tar of `public.dump`, `auth.dump` and `manifest.json`.
  The manifest reads its counts and the schema version from the same snapshot
  and adds a format number.
- **The three commands.** `harrier store backup-dump --out DIR`,
  `backup-check --dir DIR --into URL` and `backup-seal --dir DIR --recipient
  FILE --out FILE`. The workflow's steps are these commands in that order. The
  check refuses a database that has an `auth` schema or any table in `public`,
  so a wrong `--into` restores nothing. A pass leaves a marker holding the
  digest of each file, and the seal refuses a directory without it or a file
  changed since, so the order is held in code as well as in the YAML.
- **The recipient.** The committed `deploy/backup-recipient.txt` holds the
  operator's public key. The seal still refuses a file that holds the word
  `REPLACE_WITH_AGE_PUBLIC_KEY`, a missing file and anything that is not one
  `age1...` key.
- **`backup-files` writes object names.** Storage holds most files under a
  hashed name that cannot be turned back into a key, so the dated directory
  holds the object's name below the owner's prefix, not a readable key. Whether
  an object is unchanged is read from Storage's listing (size and version),
  kept in `<date>.index.json` beside the dated directory; an object whose
  listing changed but whose bytes did not is linked after the download. It
  shares spec 146's 429 waits (`artifactimport.patiently`) and the operator's
  environment reading (`operator.read_admin`), and adds
  `SupabaseFileStore.stored_objects` and `read_object` to spec 107's seam.
- **The restore goes into a new project.** Dropping `public` on a live
  project drops the trigger on `auth.users` and the Storage policy that
  depend on it, so `docs/deploy.md` restores into a new, empty project.

## Out of scope

- Point-in-time recovery, and backups more often than nightly.
- Production: none exists.
- Backing Storage up on a schedule, or in GitHub.
- Supabase's paid backups.
- Automating the restore: it is a documented procedure, run by hand.

## Honest limitations

- A lost private key makes every backup unreadable. The password manager is
  the one place it lives.
- Up to a day of writes can be lost between backups.
- Files are only as fresh as the operator's last `backup-files` run.
- The 30-day window keeps an erased owner's rows in backups for up to 30
  days after the erase, which spec 111's ledger re-erase handles on
  restore.
- GitHub may pause the schedule on a quiet repository, and nothing but the
  artifact list shows a skipped night.
- The nightly check proves a restore into a plain Postgres 17. It does not
  prove the runbook's steps on Supabase, which the operator rehearses once on a
  scratch project; the runbook says so.
- A dump carries no `storage` object (the bucket and its policy), no role and
  no role setting, and no login role's password. A restored project needs
  those made again, and no command applies the Storage statements to a
  restored store. A follow-up spec owns that command.
- The dump uses `pg_dump --snapshot` through the session pooler. Nothing here
  was run against Supabase; the first scheduled run is the first test of it.
- `backup-files` copies are named by object name. No command uploads from them
  yet: `import-artifacts` re-uploads from the local `data/` directories.

## Migration

The operator makes the `age` key pair once (`age-keygen`), stores the
private key in the password manager, and commits the public key as
`deploy/backup-recipient.txt` in the change that builds this spec.
