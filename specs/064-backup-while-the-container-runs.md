---
spec: 064
title: A backup can be taken while the container runs
status: draft
approved: yes
milestone: M8
depends: [030, 051, 061]
---

# Spec 064: A backup can be taken while the container runs

**Note, 2026-10-04: spec 061 was split.** Where this spec says "spec 061" about
the class table, the `host-path` class, or delegation, read spec 074
(`specs/074-host-commands-run-inside-the-container.md`). Spec 061 now covers
the refusal alone. Spec 074's class table carries the amendment this spec
made: `backup` without `--dest` is `database` and delegated, `backup --dest`
is `host-path`. The open criterion below about applying that amendment to
spec 061's working copy is met by spec 074's text.

- Status: Draft
- Depends on: 030 (backup and restore), 051 (container daily driver),
  061 (one kernel opens the tracker database)

## Problem

The operator runs the API in the `harrier` container all day (spec 051). There
is no working way to take a backup while it runs.

- **Inside the container, `harrier backup` crashes.** The container runs as the
  host user's uid with no passwd entry, so `HOME` resolves to `/` (the same
  cause the Dockerfile records for Playwright, `Dockerfile:87`). The default
  destination is `Path.home() / "Backups" / "harrier"`
  (`services/api/src/harrier/backup.py:79`), which becomes `/Backups/harrier`.
  `create_backup` calls `mkdir` on it (`backup.py:202`) and the process dies
  with an uncaught `PermissionError` traceback. `_cmd_backup` only catches
  `BackupError` (`services/api/src/harrier_cli/main.py:1032`). Even a writable
  path there would land inside the container's own filesystem and disappear
  with it.
- **On the host, a backup is unsafe or refused.** Reading the live database
  from the host while the container writes it is the two-kernel access spec
  061 exists to stop. Once spec 061 ships, `backup` is classed `host-path` and
  refused with exit 75 while the container runs. Spec 061 records "No backup
  while the container runs" as a limitation and names a later spec as the
  place where backing up a running system belongs. This is that spec, narrowed
  to `backup` alone.

The result today: a backup means stopping the container, so the UI goes down.
On 2026-10-01 the operator needed a backup before a bulk rescore and had to
take one by hand with the SQLite backup API and `docker cp`.

## Scope

### The backup directory is mounted

`docker-compose.yml` adds one bind mount and one environment value:

| Host | Container | Mode |
|---|---|---|
| `${HARRIER_BACKUP_HOST_DIR:-${HOME}/Backups/harrier}` | `/app/backups` | read-write |

- `environment: HARRIER_BACKUP_DIR=/app/backups` is set on the service. Compose
  gives `environment` precedence over `env_file`, so a host-side
  `HARRIER_BACKUP_DIR` in `.env` cannot leak a host path into the container.
- The host default equals the host CLI's own default (`backup.py:83`), so a
  backup taken on the host with the container stopped and one taken in the
  container land in the same directory and share one retention window
  (spec 030, `DEFAULT_KEEP = 14`).
- `just container-up` creates the host directory with `mkdir -p` before
  `docker compose up`, as it already does for `data`, `config` and `secrets`.
  Spec 051 forbids relying on Docker to create a bind source, because Docker
  creates it root-owned. The directory is the one compose resolves, read from
  `docker compose config`, so a `HARRIER_BACKUP_HOST_DIR` set only in `.env`
  is the one created. If compose cannot render its config, the recipe stops
  before `docker compose up`.

  **Amended during implementation (review finding on PR #78).** As approved,
  this said only "with `mkdir -p`". The first implementation expanded
  `HARRIER_BACKUP_HOST_DIR` in the recipe's shell, which never sees `.env`,
  while compose reads `.env` for interpolation. A value set there was mounted
  but not created.
- Files written there are owned by the host user, because the container runs
  as the host uid (`docker-compose.yml:39`).

### `harrier backup` inside the container

`docker exec harrier harrier backup` takes the same backup spec 030 describes:
snapshot through SQLite, archive, verify, prune. It writes
`/app/backups/harrier-data-<timestamp>.tar.gz` (the existing archive naming), which
the operator sees at the host directory. The snapshot is taken by the
container's kernel, the same kernel that holds the database open, so it is the
single-kernel access spec 061 requires. A backup during an open write is
already proved by
`tests/test_backup.py::test_a_backup_taken_during_an_open_write_holds_the_committed_rows`.

The printed archive path is the container path (`/app/backups/...`). The host
path is not computed inside the container.

### A destination that cannot be created is a failure, not a crash

Anywhere, host or container: when the backup directory cannot be created or
written, `harrier backup` prints one line to stderr and exits 1. The line names
which of the two failed:

```
backup failed: cannot create backup directory <path>: <OS error>. Set HARRIER_BACKUP_DIR or pass --dest.
backup failed: cannot write to backup directory <path>: <OS error>. Set HARRIER_BACKUP_DIR or pass --dest.
```

No traceback. No archive is left behind and nothing is pruned. The first line
covers the `mkdir`. The second covers writing the archive, including a
directory that exists but is not writable (the root-owned case below).

**Amended during implementation.** As approved, this section gave only the
first line and said it applied to both cases. "cannot create" is untrue for a
directory that exists, so writing the archive got its own wording.

### Under spec 061

This amends spec 061's class table. The text applied to spec 061 in the same
change that implements this one:

- `backup` with no `--dest` is a `database` command. While the container runs,
  the host CLI delegates it, and the backup is taken inside the container as
  described above.
- `backup --dest <path>` stays `host-path`: refused with exit 75 while the
  container runs. A host path does not exist inside the container.
- `--keep` is not a path argument and does not change the class.
- The sentence "`backup` is always `host-path`, because its default
  destination is outside the mounts" is replaced by the two rules above.
- The limitation "No backup while the container runs" is removed. Its
  replacement is the limitation below about two backup directories.

If spec 061 has not shipped when this lands, there is no class table and no
delegation. The container-side behavior still works through
`docker exec harrier harrier backup`. The class table amendment is applied to
spec 061's text either way, so 061 is implemented against the amended table.

### Spec 051 mount table

Spec 051's "What is mounted" table gains the backup row above. The text is
applied to spec 051 in the same change.

## Failure modes

- **The host directory does not exist** because the operator started the
  container with plain `docker compose up`, not `just container-up`. Docker
  creates it root-owned and the backup fails with the `cannot write to backup
  directory /app/backups` line above. It does not crash.
- **`HARRIER_BACKUP_HOST_DIR` is set only in `.env`.** Compose mounts that
  directory, and `just container-up` creates that same directory, because it
  asks compose rather than expanding the variable itself.
- **`HARRIER_BACKUP_HOST_DIR` is set but the host CLI's `HARRIER_BACKUP_DIR` is
  not, or the two differ.** Container backups and host backups go to different
  directories and each prunes only its own. Nothing is lost. This is named as
  a limitation, not prevented.
- **The container is stopped.** Nothing changes: the host CLI backs up to its
  own default as it does today.
- **Two backups started in the same second.** The existing guard refuses the
  second (`backup.py:204`, "an archive already exists"). Unchanged.
- **Demo mode.** `HARRIER_DEMO=1` writes its data to a temp directory
  (`services/api/src/harrier/demo.py:42`, spec 021). A backup in demo mode archives the demo data into the mounted
  directory. That is the existing spec 030 behavior with the destination
  moved, not a new leak: the archive holds demo data only.
- **The archive is restored.** Restore is not changed. An archive in the host
  directory restores on the host with the container stopped, as spec 030
  describes.

## Acceptance criteria

- [x] `docker compose config` shows the `/app/backups` bind mount with source
      `${HOME}/Backups/harrier` when `HARRIER_BACKUP_HOST_DIR` is unset, and
      `HARRIER_BACKUP_DIR=/app/backups` under the service's environment.
- [x] `just container-up` on a machine with no `~/Backups/harrier` creates it,
      owned by the host user, before the container starts.
- [x] With the container running, `docker exec harrier harrier backup` exits 0
      and prints `/app/backups/harrier-data-<timestamp>.tar.gz (... verified)`. The
      archive exists in `~/Backups/harrier` on the host, owned by the host
      user, and `harrier verify-backup <that archive>` on the host exits 0.
      By hand, transcript in the pull request with identity values redacted.
- [x] `HARRIER_BACKUP_DIR` pointing at a directory that cannot be created makes
      `harrier backup` exit 1 with the one-line `backup failed: cannot create
      backup directory ...` message and no traceback. Proved by
      `services/api/tests/test_backup.py::test_an_uncreatable_backup_directory_fails_with_one_line`,
      which runs the CLI entry point with the `mkdir` of that directory
      refused with `PermissionError`. The test fails on today's code, which
      raises `PermissionError`.

      **Amended 2026-10-06.** As approved, the test refused the `mkdir` with a
      read-only parent. Root ignores directory modes, so the test skipped as
      root and this criterion went unproved there. It now refuses the call
      itself, as root and as any other user.
- [x] The same failure leaves no partial archive and prunes nothing. Same test.
- [ ] Spec 061's class table text is amended as stated above, in the same
      change. Open: spec 061 is not yet in git, so the amendment is applied
      to its working copy and lands when spec 061 is committed.
- [ ] Once spec 061's delegation exists: on the host with the container
      running, `harrier backup` is delegated and exits 0, and
      `harrier backup --dest /tmp/x` exits 75. Proved by spec 061's class
      table tests, extended with these two vector cases.
- [x] Spec 051's mount table lists the backup mount.

## Proof / origin

- The crash: `docker exec harrier harrier backup` on 2026-10-01 ended in
  `PermissionError: [Errno 13] Permission denied: '/Backups'`, raised by the
  `mkdir` in `create_backup` (`services/api/src/harrier/backup.py:202`).
- The default that produces `/Backups`: `backup_dir`
  (`services/api/src/harrier/backup.py:79`) with `HOME` resolving to `/` for a
  uid with no passwd entry (`Dockerfile:87`).
- The uncaught error: `_cmd_backup` catches only `BackupError`
  (`services/api/src/harrier_cli/main.py:1032`).
- The deferral this spec answers: spec 061, "Out of scope", path translation
  for `host-path` commands, "That spec is also where backing up a running
  system belongs."

## Out of scope

- **Restore while the container runs.** Restore replaces the data directory
  under a live API. It stays host-only with the container stopped.
- **Path translation for other `host-path` commands** (`export`,
  `profile import`, `--jd-file` and the rest). Spec 061 keeps them refused.
- **Translating the printed archive path to the host path.**
- **Scheduling backups.** Spec 030 does not schedule them and neither does this.
- **Off-machine or encrypted backups.** Spec 030 already defers these.
- **Changing `HOME` in the image** or adding a passwd entry. That would change
  every other tool's view of `HOME` (Playwright, the `claude` CLI), which is
  its own change.
- **Fixing the 152 items `harrier check` reports.** Unrelated to backup.

## Honest limitations

- **Two directories are possible.** If the operator sets the host and container
  backup directories to different places, retention runs separately in each.
- **The printed path is the container's.** The operator has to know that
  `/app/backups` is `~/Backups/harrier`. The compose file comment says so.
- **A backup taken through delegation depends on the image being current.**
  Spec 061 already records that delegated runs use the image's revision.

## Migration

For the one operator:

1. Run `just container-up` once. It creates `~/Backups/harrier` if missing and
   recreates the container with the new mount.
2. Existing archives in `~/Backups/harrier` stay where they are and count
   toward retention as before.
3. No schema change. No data change. No change to `config/data-classification.json`:
   the new path is outside the repository, and the only new in-repo surface is
   `docker-compose.yml` and `justfile`, both already classified public.
