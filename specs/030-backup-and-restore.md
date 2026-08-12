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
second most severe finding because it is the most total loss available.

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

Proven by services/api/tests/test_backup.py:

| Criterion | Proof |
|---|---|
| a backup during an open write holds every committed row | `tests/test_backup.py::test_a_backup_taken_during_an_open_write_holds_the_committed_rows` |
| the data directory override is honoured | `tests/test_backup.py::test_the_backup_follows_the_data_directory_override` |
| an unopenable archive fails and evicts nothing | `tests/test_backup.py::test_a_corrupted_archive_is_rejected`, `tests/test_backup.py::test_a_truncated_database_inside_an_archive_is_rejected` |
| verification asks a question a torn database fails | `tests/test_backup.py::test_verification_asks_a_question_about_content` |
| restore refuses a non-empty target without the flag | `tests/test_backup.py::test_restore_refuses_a_non_empty_directory`, `tests/test_backup.py::test_restore_overwrites_when_forced` |
| a verified archive restores to a readable tracker | `tests/test_backup.py::test_a_verified_archive_restores_to_a_readable_tracker` |
| retention keeps the configured number and the newest of each week | `tests/test_backup.py::test_retention_keeps_the_configured_number`, `tests/test_backup.py::test_the_newest_of_each_week_survives_pruning`, `tests/test_backup.py::test_retention_drops_the_oldest_first`, `tests/test_backup.py::test_pruning_refuses_to_keep_nothing` |
| nothing about a machine or an account is committed | the archive is written outside the repository, and nothing here writes to a tracked file (ADR-008) |

Beyond the criteria, two behaviours were found while implementing and are
tested rather than left implicit: a broken archive leaves the target
directory untouched, because verification runs before anything is moved
(`tests/test_backup.py::test_restore_of_a_broken_archive_leaves_the_target_alone`); and an archive
member that would escape the target is refused, because a restore is exactly
the moment somebody points the command at a file they were sent
(`tests/test_backup.py::test_an_archive_escaping_its_target_is_refused`).

A later review of this branch found five defects in the restore and
retention paths, and they are recorded here because each is a way the command
could have lost the data it exists to protect.

Retention implemented only half of the policy above: the count, not the
weekly. Fourteen nightly archives cover fourteen days, so a corruption
noticed a fortnight late had nothing behind it
(`tests/test_backup.py::test_the_newest_of_each_week_survives_pruning`).

The restore deleted the target before the replacement was in place, and moved
the payload from the system temp directory, which is often another
filesystem. A cross-filesystem move is a copy, and a copy that failed partway
left no usable directory at all. The payload is now staged beside the target
so the install is a rename, and the previous directory is kept until that
rename succeeds
(`tests/test_backup.py::test_a_failed_replacement_puts_the_old_data_directory_back`,
`tests/test_backup.py::test_a_failed_rollback_says_where_the_data_directory_went`).

The archive was opened twice: once to verify it, once to install it. Only the
first read was checked. One extraction is now verified and installed
(`tests/test_backup.py::test_the_restored_payload_is_the_payload_that_was_verified`).

A failed archive was left on disk, where the next prune counted it towards
`keep` and could delete a verified archive to make room for one that would
not open (`tests/test_backup.py::test_a_failed_archive_is_not_left_behind`).

A restore into a path that was an existing file raised `NotADirectoryError`,
which the command does not catch, so a mistyped path produced a traceback
(`tests/test_backup.py::test_restore_into_an_existing_file_is_refused`).

One test in the first version of this suite could not fail, and it is
recorded because the failure mode is the one this spec exists to close. It
wrote a `tracker.db-wal` file by hand and asserted the archive did not
contain it. That passed whatever the code did, since SQLite removes the
write-ahead log when the snapshot connection closes, so the file was gone
before the copy loop ran. Rewritten to use a stray file SQLite does not
manage, and paired with a test that the archived database is the snapshot
rather than the live file.

- [x] a backup taken while a write transaction is open restores to a database
      containing every committed row, and none of the uncommitted one
- [x] a backup taken with `HARRIER_DATA_DIR` set archives that directory
- [x] an archive that cannot be opened fails the run with a non-zero exit and
      does not evict the previous archive
- [x] verification asks a question a torn database would fail, proven by a
      test that corrupts an archive and sees it rejected
- [x] restore into a non-empty data directory is refused without the flag
- [x] restore of a verified archive produces a tracker the CLI can read
- [x] retention keeps the configured number and never deletes the newest
      verified archive
- [x] no archive path, machine name, or account name is written to any
      committed file (ADR-008)
- [ ] All gates green on PR

## Proof / origin

The `principal-review` board, spec 028. The WAL exclusion, the unconditional
exit, and the absence of any restore command are each verifiable in the tree.

## Out of scope

Off-machine or encrypted-at-rest backup, both of which belong with the
threat model rather than here. Scheduling: this spec makes the backup
trustworthy, not automatic. Point-in-time recovery.
