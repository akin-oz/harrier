---
spec: 030
title: A backup that can be restored, and a restore that is exercised
status: accepted
approved: yes
milestone: M6
depends: [003]
---

# Spec 030: A backup that can be restored, and a restore that is exercised

## Problem

scripts/backup.sh archives `data/` with `tar -czf` while excluding
`data/*.db-wal` and `data/*.db-shm`. services/api/src/harrier/db.py opens the
database with `PRAGMA journal_mode=WAL`, so committed transactions since the
last checkpoint live in exactly the file the archive omits. tar also reads a
file being written page by page, so the main database can be captured torn.

The result is an archive that exits 0 and may not open.

Two things compound it. ADR-008 says a new machine is provisioned by
restoring the database, and no command implements a restore, so the recovery
path has never been executed. And backup.sh resolves `data/` by its own rule
while db.py honours `HARRIER_DATA_DIR`, so with the override set the script
archives an empty directory and reports success.

After cutover this is the only copy of a real person's job search.

Found by the `principal-review` board (spec 028), operability lens, rated its
second most severe finding on the grounds that it is the most total loss.

## Scope

**Back up through SQLite, not around it.** Use the online backup API or
`VACUUM INTO`, which take a consistent snapshot of a live database including
everything in the write-ahead log. The tar of loose files stays for the
non-database contents of `data/`.

**One data directory rule.** The backup resolves the data directory the same
way the application does, `HARRIER_DATA_DIR` included. Backing up a different
directory from the one in use is a silent total failure and must be
impossible rather than documented.

**Verify the archive.** Every backup is opened after it is written and asked
a question only a working database can answer. An archive that fails
verification is reported as a failure and does not replace the previous one.

**A restore command.** `harrier restore` takes an archive and produces a
working data directory, refusing to overwrite a non-empty one without an
explicit flag. The recovery path is a command with tests, not a paragraph.

**Retention.** Bounded: keep the most recent N and the most recent weekly,
so a repeating failure cannot fill the disk and a single bad night cannot
evict every good copy.

## Inputs, outputs, failure modes

- Inputs: the resolved data directory, a destination directory, a retention
  count.
- Outputs: one archive per run, a verification result, and a non-zero exit
  when either the copy or the verification failed.
- Failure modes that must be surfaced rather than absorbed: destination not
  writable, disk full mid-write, source database locked by a long
  transaction, archive verifies as unopenable.
- Failure mode this must not introduce: a verification step that reports
  success because it asked nothing. The check runs a query whose result
  depends on real content, not `PRAGMA integrity_check` alone.
- Restore refuses rather than merges. Restoring into a directory that already
  holds a tracker is how two half-populated databases are created.

## Acceptance criteria

Proving symbols are named at implementation, in
services/api/tests/test_backup.py.

- [ ] a backup taken while a write transaction is open restores to a database
      containing every committed row, and none of the uncommitted one
- [ ] a backup taken with `HARRIER_DATA_DIR` set archives that directory
- [ ] an archive that cannot be opened fails the run with a non-zero exit and
      does not evict the previous archive
- [ ] verification asks a question a torn database would fail, proven by a
      test that corrupts an archive and sees it rejected
- [ ] restore into a non-empty data directory is refused without the flag
- [ ] restore of a verified archive produces a tracker the CLI can read
- [ ] retention keeps the configured number and never deletes the newest
      verified archive
- [ ] no archive path, machine name, or account name is written to any
      committed file (ADR-008)
- [ ] All gates green on PR

## Proof / origin

The `principal-review` board, spec 028. The WAL exclusion, the unconditional
exit, and the absence of any restore command are each verifiable in the tree.

## Out of scope

Off-machine or encrypted-at-rest backup, both of which belong with the
threat model rather than here. Scheduling: this spec makes the backup
trustworthy, not automatic. Point-in-time recovery.
