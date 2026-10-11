---
spec: 129
title: A stored deadline can be corrected by hand
status: accepted
approved: yes
milestone: M9
depends: [027, 074, 092, 093, 097, 101, 112, 126]
---

# Spec 129: A stored deadline can be corrected by hand

## Problem

An academic row's `deadline` is written once, when the row is added, and
nothing can change it afterwards.

- `add --deadline` (spec 093) and discovery (spec 097) set it in the
  insert (`services/api/src/harrier/tracker/store.py`, `add_job`).
- `update_fields` (`tracker/store.py:471`) is the only path that changes
  non-status columns, and it refuses `deadline`: it allows
  `TRACKER_FIELDS` and `NOTE_KEYS` only, and `deadline` is in neither
  (spec 093 keeps it out of `TRACKER_FIELDS` on purpose).
- A later discovery run that returns the same posting rejects it as a
  duplicate and leaves the stored row as it was (spec 097, "Dedupe").
  The posting window usually stops returning it anyway.

So a wrong deadline stays wrong. Spec 097's amendment of 2026-10-11 found
that one portal's listing date is later than the call's own closing date
on every item checked, and that rows already stored keep the late date;
its Migration hands the correction to this spec (its open decision 4).
The same gap holds for a hand-added row whose call is extended, or whose
date was typed wrong.

The cost falls on the operator. The queue orders by the stored date
(spec 093), the `deadline_passed` gate reads it (spec 097), and spec
101's warning stays silent until it passes. A late date makes a closed
call look open.

The only workaround today is an SQL write to the database, which the one
write path rule (ADR-003) forbids.

## Scope

Files this spec changes:

- `services/api/src/harrier/tracker/store.py`: `set_deadline`.
- `services/api/src/harrier/tracker/actions.py`: the action the command
  calls, resolving the selector and refusing the industry kind.
- `services/api/src/harrier/tracks.py`: `deadline` in
  `NON_DEFAULT_OPERATIONS` and `WRITE_OPERATIONS`.
- `services/api/src/harrier_cli/main.py`: the `deadline` subcommand and
  its `COMMAND_CLASSES` entry.
- `README.md`: the academic track paragraph.
- Tests: `services/api/tests/test_tracks_cli.py`,
  `services/api/tests/test_tracker_store.py`,
  `services/api/tests/test_postgres_store.py`.

No schema, migration, route, contract or classification change.

## Behavior

### The command

```
harrier --track <slug> deadline <selector> YYYY-MM-DD
harrier --track <slug> deadline <selector> --clear
```

- `<selector>` is a job id or a unique substring, resolved as every
  tracker verb resolves it (spec 027, `harrier.tracker.selector`), within
  the scope's track only (spec 092).
- The date is parsed by the same `_iso_date` type that `add --deadline`
  and `applied --applied-date` use
  (`services/api/src/harrier_cli/main.py:1138`). `--clear` stores the
  empty string, which means no deadline (spec 093).
- Exactly one of a date and `--clear` is given.

### What it writes

- One new store function, `set_deadline(conn, scope, job_id, deadline)`
  in `services/api/src/harrier/tracker/store.py`, is the write. It checks
  the value with `_is_iso_date` or accepts the empty string, refuses an
  archived track as `update_fields` does, and runs under `_write_lock`.
  It updates `deadline` and `updated_at` on the one row of the scope's
  track, nothing else, in one transaction. `updated_at` is written the
  way spec 126 says domain timestamps are written.
- `update_fields` is unchanged: it still refuses `deadline`. The deadline
  has one write path after insert, and it is this one.
- A date equal to the stored one writes nothing, and `updated_at` is
  unchanged.
- Any status may be corrected, `rejected` included. Nothing about the
  status, the notes or the row's events changes.

### What it prints

On success, to stdout, exit 0:

```
job <id>: deadline <new>, was <old>
```

where an empty value reads `none`. An unchanged date prints
`job <id>: deadline <date>, unchanged`. Then the row is printed as the
status verbs print it (`_print_job`), so the queue's `deadline passed`
flag is visible at once.

### Where it runs

- On an academic track only. On an industry track it is refused, exit 2:
  `refused: deadline is not available on track <slug>`. Industry rows
  carry no deadline (spec 097, "Dedupe"), and nothing on that kind reads
  one.
- `deadline` joins `NON_DEFAULT_OPERATIONS` and `WRITE_OPERATIONS`
  (`services/api/src/harrier/tracks.py:225`, `:258`), so an archived
  track refuses it with the existing message.
- It joins `COMMAND_CLASSES` as a database command
  (`services/api/src/harrier_cli/main.py:2275`), so on the host it runs
  inside the container like every other tracker verb (spec 074).
- It runs on whichever store `connect` returns (spec 112). There is no
  API route and no browser control.

### What reads the new date

Everything that reads the row reads the corrected date on its next read:
the academic queue and its flag (spec 093), the `deadline_passed` gate and
the deadline-aware dedupe on the next discovery run (spec 097), and the
passed-deadline warning (spec 101). None of them changes.

The README's paragraph on academic tracks (`README.md:254-265`) names the
command beside `add --deadline`, and adds `deadline` to its list of
commands that run on a track other than the default.

## Failure modes

- **A malformed or impossible date.** Exit 2 from the parser, as for
  `add --deadline`. Nothing is written.
- **Neither a date nor `--clear`, or both.** Exit 2 with a usage error.
- **No row or more than one row matches the selector.** Exit 1 with the
  selector's message, as the status verbs exit
  (`harrier_cli/main.py:1338-1340`).
- **A row of another track.** Not found, with the same message as a
  missing id (spec 092).
- **The industry track.** Exit 2, named above. Nothing is written.
- **An archived track.** Exit 2, `track <slug> is archived`.
- **A value that reaches the store without the parser** (a caller in
  code). `set_deadline` raises `TrackerError` naming the value's shape;
  the column's CHECK (spec 093) is the backstop.
- **Two writers at once.** `_write_lock` serializes them; the last write
  wins, and each prints the date it replaced.
- **The corrected date changes a dedupe outcome.** The organisation and
  title rule treats two academic rows as one call when their deadlines are
  equal or either is empty (spec 097). Correcting a row's date can make a
  later posting of the same title at the same organisation a duplicate, or
  stop it being one. That is the rule reading the true date, and it is
  not special-cased.
- **A container running an image from before this spec.** The child's
  parser does not know `deadline` and exits 2 with its usage error.
  `just container-up` is the fix (spec 074).

Must not introduce: a change to `update_fields`; a deadline write on an
industry row; a status, note or event written by this command; a route
or contract change; a log line carrying a date, a company or a title.

## Acceptance criteria

Every database is built under `tmp_path`, with synthetic rows on an
invented institution. Nothing reads the operator's database.

- [ ] The command sets an academic row's deadline and prints the old and
      new dates (planned test_deadline_sets_and_prints_the_old_date, in
      `services/api/tests/test_tracks_cli.py`)
- [ ] `--clear` stores the empty string and prints `none`
      (planned test_deadline_clear_stores_no_deadline)
- [ ] An unchanged date writes nothing and leaves `updated_at` as it was
      (planned test_an_unchanged_deadline_writes_nothing)
- [ ] The queue orders by the corrected date at once: a row moved to a
      passed date sinks and is flagged
      (planned test_a_corrected_deadline_reorders_the_queue)
- [ ] The industry track refuses the command with exit 2 and writes
      nothing (planned test_deadline_is_refused_on_the_industry_track)
- [ ] An archived track refuses it with exit 2
      (planned test_deadline_is_refused_on_an_archived_track)
- [ ] A malformed date, no value, and both a date and `--clear` each exit
      2 (planned test_deadline_refuses_a_malformed_or_missing_value)
- [ ] A selector naming another track's row exits 1 as not found
      (planned test_deadline_does_not_reach_another_tracks_row)
- [ ] `set_deadline` changes only `deadline` and `updated_at`: status,
      notes and the row's events are unchanged
      (planned test_set_deadline_writes_only_the_deadline, in
      `services/api/tests/test_tracker_store.py`)
- [ ] `set_deadline` refuses a value that is neither empty nor a
      `YYYY-MM-DD` date (planned test_set_deadline_refuses_a_malformed_value)
- [ ] `update_fields` still refuses `deadline`
      (planned test_update_fields_still_refuses_the_deadline)
- [ ] `set_deadline` behaves the same on the Postgres store
      (planned test_set_deadline_on_the_postgres_store, in
      `services/api/tests/test_postgres_store.py`, skipped where no
      Postgres URL is configured, as that file's tests are)
- [ ] `deadline` is a database command, so the host delegates it to the
      container (planned test_deadline_is_a_database_command)
- [ ] The README names the command and lists it among the commands that
      run on a non-default track. No test reads that paragraph; the
      reviewer checks it against the diff

## Honest limitations

- **No history of the old date.** `job_events` records status decisions,
  and its `kind` CHECK allows `created`, `decision` and `outcome` only
  (`tracker/schema.py:316`). A deadline change is not one of them. The
  old date is printed and then gone. Recording it is open decision 1.
- **The command is only as right as the date typed.** It does not read
  the call; the operator does.
- **No bulk correction.** One row per command. Spec 097's Migration asks
  the operator to check each open row from a checked portal; with few
  rows, one command each is enough.
- **Command line only.** The browser shows the deadline (spec 094) but
  cannot change it.

## Out of scope

- A browser control or API route for the deadline, and the contract
  change it needs.
- A history event for a deadline change, or any `job_events` migration.
- Setting a deadline on an industry row.
- Several dates per call (spec 093, Honest limitations).
- Discovery rewriting a stored row's deadline when a later run returns
  the posting.
- Correcting any other column after insert.

## Open decisions for Akin

Taken 2026-10-11: Akin approved the spec as written, in chat ("approve
spec 129"), so each recommendation below stands.

1. **A history of the old date.** (a) None; the command prints it.
   (b) A `deadline_was=<date>` note on the row. (c) A new `job_events`
   kind, `deadline`, which is a migration on both stores. Recommendation:
   (a). The date matters for the queue, not as a record, and (c) is a
   schema change for one field.
2. **The selector.** A job id or a unique substring, like every tracker
   verb; or a job id only. Recommendation: the selector, for consistency.
3. **The industry track.** Refused, as above; or allowed and ignored.
   Recommendation: refused, because a value nothing reads is a trap.

## Migration

None for data. After it ships, correct each academic row whose stored
date is wrong with `harrier --track <slug> deadline <id> YYYY-MM-DD`.

## Proof / origin

- The write path that refuses `deadline`:
  `services/api/src/harrier/tracker/store.py:471` (`update_fields`); the
  insert that sets it: `add_job` in the same file; the column and its
  CHECK: spec 093 and `tracker/schema.py`.
- The request: spec 097, Amendment (2026-10-11), Migration and open
  decision 4.
- The track allowlists: `services/api/src/harrier/tracks.py`
  (`NON_DEFAULT_OPERATIONS`, `WRITE_OPERATIONS`); the delegation table:
  `services/api/src/harrier_cli/main.py` (`COMMAND_CLASSES`), spec 074.
- The selector and its exits: `services/api/src/harrier/tracker/selector.py`,
  `harrier_cli/main.py:1338-1340`.
- The readers of the date: `services/api/src/harrier/tracker/queue.py`
  (`rank_by_deadline`, `deadline_warning`), spec 097's gate and dedupe.
