---
spec: 061
title: The host refuses to open the tracker database while the container owns it
status: accepted
approved: yes
milestone: M8
depends: [020, 030, 051, 060]
---

# Spec 061: The host refuses to open the tracker database while the container owns it

**Split on 2026-10-04.** The 2026-09-18 draft of this spec covered the whole
response to the corruption in one change: a guard, delegation into the
container, a host lease, API and web changes, and a new command. Reviewed on
2026-10-04, it was too large for one reviewable change, and the repository
rule is to split the spec rather than the branch. It is now three specs:

- **061 (this one): the guard.** The host never opens the live database while
  the container owns it, and says so. This alone removes the class of access
  that corrupted the file.
- **074: delegation.** Host commands run inside the container instead of being
  refused, so the launchd schedule works while the container is up.
- **075: the host lease.** Closes the window where the container starts in the
  middle of a host run, and gives the API and the web app a way to say so.

The draft's approval covered text that was later amended ten times and is now
replaced. This spec needs approval on its own text.

## Problem

On 2026-09-18 `data/tracker.db` was corrupted. The operator rebuilt it from a
snapshot plus rows recovered with `sqlite3 .recover`. The corrupt file is kept
locally under `data/` (never in git) for inspection.

What was running:

- The `harrier` container, with `./data` bind-mounted at `/app/data`
  (`docker-compose.yml`), writing job rows and rejections.
- On the macOS host, a profile document update through `put_document`.
- On the macOS host, the pytest suite, which then reached the default
  database through `harrier.logsetup` (fixed since by spec 060).

What the corrupt file shows, reproduced by opening it with
`file:...?immutable=1` and running `PRAGMA integrity_check`: bytes 18 and 19
of the header are `02 02` (WAL), two "invalid page number" lines, two "2nd
reference to page" lines, three "never used" pages, and wrong entry counts in
`idx_jobs_status` and `idx_jobs_company_title`.

The cause is structural. `harrier.db.connect` sets `PRAGMA journal_mode=WAL`
(`services/api/src/harrier/db.py:60`). WAL coordinates processes through a
shared memory mapping of the `-shm` file, and SQLite's documentation says all
processes using a WAL database must be on the same host
(https://sqlite.org/wal.html). The container runs under the Docker Desktop
Linux VM; the host CLI runs under the macOS kernel. They reach one file
through a bind mount and do not share memory.

Spec 051 chose "WAL plus a busy timeout" and proved it with
`services/api/tests/test_concurrent_writers.py`, which runs two processes
under one kernel. Its own limitation said this "does not by itself prove
Docker Desktop's bind mount honours the same locking". The incident is that
limitation arriving.

Two properties make it worse than "two writers":

1. **Readers are unsafe too.** A WAL reader uses the shared index, and a
   connection that believes it is the last one checkpoints and removes the
   WAL on close. The rule below covers every open, not only writes.
2. **Nothing refuses.** Every process succeeded, with exit code 0, until
   `integrity_check` said otherwise.

Today the only protection is the operator remembering. The launchd schedule
is not installed at the time of writing, which removes the continuous
exposure the scheduled jobs created, but every host command that opens the
database (`harrier shortlist`, `harrier config set`, `harrier backup`, a
`uv run python` session) still opens it while the container runs, and still
exits 0.

## Options weighed

Three responses were weighed in the 2026-09-18 draft:

- **A. The host CLI becomes a client of the container's API.** Not taken: 26
  `connect()` call sites in `services/api/src/harrier_cli/main.py` would each
  need an endpoint, a contract entry and a client, a second implementation of
  the CLI that would drift.
- **B. The host refuses, and later delegates into the container.** Taken.
  This spec is the refusal; spec 074 is the delegation.
- **C. Drop WAL for a rollback journal.** Not taken: rollback mode depends on
  POSIX locks being honoured across the bind mount, which spec 051 called
  unreliable and nobody has verified. It swaps a documented unsupported
  configuration for an unverified one, and the failure mode stays
  corruption.

## Scope

- `services/api/src/harrier/db.py`: the ownership check and the guarded
  `connect`.
- `services/api/src/harrier/backup.py`: its direct `sqlite3.connect` calls on
  the live path (`:102`, `:121`) go through the guard, and `create_backup` and
  `restore_backup` call the ownership check.
- `services/api/src/harrier/logsetup.py`: the refusal is not swallowed.
- `services/api/src/harrier_cli/main.py`: the refusal becomes exit 75, and the
  new `doctor` command.
- Tests under `services/api/tests/`.
- Records: `specs/051-container-daily-driver.md`, the comment in `db.py`, a
  new `docs/adr/ADR-011-one-kernel-owns-the-database.md`, and the README's
  operator section.

The API, the contract, `apps/web` and the schedule do not change.

## Behavior

### The rule

At any moment, processes under exactly one kernel have `data/tracker.db`
open. While the `harrier` container is running with this repository's
`data/` mounted, that kernel is the container's. Otherwise it is the host's.

"Open" means any SQLite open of the live file, read or write.

"The live file" means a database path that resolves inside the host
directory the running container has mounted at `/app/data`. A database under
a pytest `tmp_path`, a demo temp directory (spec 021), or an extracted backup
is not live and is never refused.

### Detecting the container

The process asks the Docker engine whether a container named `harrier` is
running and what it has mounted at `/app/data`. The lookup must work in
launchd's environment, which has a minimal `PATH`. How (the engine socket, or
a resolved `docker` binary with an env override in the style of
`CLAUDE_CLI_PATH`) is an implementation choice recorded by amendment. The
lookup has a bounded timeout.

| Engine says | The process may open the live file |
|---|---|
| engine not reachable (no socket, connection refused) | yes |
| no `harrier` container, or not running | yes |
| running, `/app/data` source is this repository's `data/` | no |
| running, `/app/data` source is another directory | yes |
| engine reachable but the query errors or times out | no. Unknown is refused. |

Inside the container the engine is not reachable (no socket is mounted,
`docker-compose.yml`), so the container's own opens fall in the first row
and are allowed with no special case.

### The guard

The ownership check is one function in `harrier.db`.

- `connect` calls it before opening a live path.
- `backup.snapshot_database` and `backup.verify_database` stop calling
  `sqlite3.connect` on the live path directly and go through the guard.
- `backup.create_backup` and `backup.restore_backup` call it before they
  copy, archive, replace or delete the live file or its `-wal` and `-shm`
  files. `restore_backup` replaces the data directory without ever calling
  SQLite, so the check is the only thing that can stop it.

When the check refuses:

- Container owns the file: `DatabaseOwnedByContainer`. The message names the
  container, says the command can run inside it (`docker exec harrier harrier
  <subcommand>`), and names `harrier doctor`.
- Ownership unknown: `DatabaseOwnershipUnknown`, with the engine error in the
  message.
- The harrier CLI exits 75 for either, with the message on stderr.
- The refusal is never swallowed. The new errors are not `sqlite3.Error`,
  `OSError`, `RuntimeError` or `ValueError`, which `logsetup` and the CLI
  catch broadly.
- **Amended at implementation: logging setup.** The approved text had
  `configure_logging` let the refusal through. The CLI sets up logging
  before it parses arguments, and logging setup opens the database to load
  the redaction values, so that refused every host command while the
  container ran, `harrier doctor` and `review-followup` included. Instead,
  when the database is refused, `configure_logging` logs to stderr only,
  without redaction, opens no `data/logs/harrier.log`, and carries on. The
  refusal still happens: a command that opens the database is refused at its
  own open and exits 75. Chosen by Akin on 2026-10-04.
- There is no override flag or env var. The way to get host access is to stop
  the container.

### `harrier doctor`

A new read-only command. It never opens the live file with SQLite unless the
check allows it.

It prints, one line each:

- where it is running (host or container)
- engine reachability, the `harrier` container state, and whether its
  `/app/data` mount is this repository's `data/` (`this repository` or
  `elsewhere`, never the absolute path, which contains a home directory)
- the journal mode read from header bytes 18 and 19 with a plain file read,
  and whether `-wal` and `-shm` files exist
- the verdict: `host access: allowed`, `host access: refused (container owns
  the database)`, or `host access: refused (ownership unknown: <reason>)`

Exit codes: 0 when ownership is known, whichever side owns the file. 1 when
ownership is unknown.

- `harrier doctor --require-host-access` exits 0 only when the verdict is
  `allowed`, and 75 otherwise. It is the check to put in front of anything
  the guard cannot see, such as a raw `sqlite3 data/tracker.db` session.
- `harrier doctor --integrity` runs `PRAGMA integrity_check` when the check
  allows the open, prints the result, and exits 1 unless it is `ok`. When the
  container owns the file it does not open it; it prints the `docker exec`
  command that runs the same check inside the container, and exits 75.
  Spec 074 makes it delegate instead.

### Records

- Spec 051's "Two writers on one SQLite file" amendment gains a dated note
  that this spec supersedes it.
- The comment in `db.py` that says WAL lets the two processes interleave is
  corrected.
- ADR-011 lands with this spec, status proposed until Akin accepts it: one
  kernel opens the database; while the container runs it is the container's.
  It supersedes the database line in ADR-010's Consequences and does not
  reopen ADR-010's decision.
- README: the operator section says host access means stopping the container
  and names `harrier doctor`.

## Failure modes

- **Engine hangs** (Docker Desktop resuming from sleep). The lookup times
  out and the open is refused with exit 75. A loud refusal, not a silent
  corruption.
- **Container running but unhealthy.** It still counts as running. The host
  is refused.
- **Fresh clone, no Docker.** Engine not reachable, so host access is allowed
  and nothing changes. CI is this case.
- **Demo mode.** Never live, never refused (spec 021).
- **Host code that is not the CLI** (`uv run python`, a host uvicorn): the
  guard in `connect` refuses it the same way. It raises; there is no exit
  code to set.
- **A second run.** The check is stateless. Running the same command twice
  behaves the same way twice.

Failure modes this must not introduce:

- A host process that hangs forever waiting on the engine.
- A path by which the refusal is caught and ignored.
- The test suite needing Docker.
- An absolute host path in `harrier doctor` output.

## Acceptance criteria

Proving symbols are named at implementation. Every automated test uses a
fake detector.

Tests in `services/api/tests/test_database_ownership.py` unless named
otherwise.

- [x] with a detector reporting "running, mounted here", `connect()` on the
      live path raises `DatabaseOwnedByContainer` and `sqlite3.connect` is
      never called, asserted by a test that fails if the open happens:
      `test_connect_is_refused_before_sqlite_opens_anything`
- [x] the same for `backup.snapshot_database` and `backup.verify_database` on
      the live path:
      `test_the_backup_snapshot_and_verify_are_refused_on_the_live_file`
- [x] `create_backup` and `restore_backup` with that detector raise
      `DatabaseOwnedByContainer`, and the data directory is byte-identical
      afterwards: `test_create_and_restore_are_refused_and_touch_nothing`
- [x] with a detector reporting an engine error or timeout, the open raises
      `DatabaseOwnershipUnknown`; with the engine unreachable, it succeeds:
      `test_an_engine_that_cannot_answer_refuses_and_no_engine_allows`
- [x] a path under `tmp_path`, a demo directory, or a different mount source
      is never refused:
      `test_a_database_outside_the_mounted_directory_is_never_refused`
- [x] (amended) under refusal, `configure_logging` adds no file handler and
      writes no `harrier.log`, and the command's own open is still refused,
      proven by a test that exercises `configure_logging`:
      `test_logging_setup_under_refusal_writes_no_log_file_and_the_open_is_still_refused`
- [x] a refused CLI command exits 75 and its stderr names the container,
      the `docker exec` form, and `harrier doctor`, and no argument value:
      `test_a_refused_command_exits_75_and_names_the_way_out_without_its_arguments`,
      `test_a_nested_subcommand_is_named_in_full`,
      `test_an_unknown_owner_refuses_with_the_reason`
- [x] the detector's lookup gives up within its bound against an engine that
      never answers, proven with a real socket that accepts and never replies:
      `test_an_engine_that_never_answers_is_given_up_on_within_the_bound`;
      the engine's answers are read from a fake engine on a real socket:
      `test_the_detector_reads_the_engine_answer`,
      `test_a_socket_with_nothing_listening_is_unreachable`
- [x] `harrier doctor` prints each line listed above, exits 0 for both known
      ownership states and 1 for unknown; no line contains an absolute path:
      `test_doctor_reports_each_line_and_exits_0_whoever_owns_the_file`,
      `test_doctor_exits_1_when_ownership_is_unknown`
- [x] `harrier doctor --require-host-access` exits 75 when the container owns
      the file and 0 when the engine is unreachable:
      `test_require_host_access_exits_75_unless_access_is_allowed`
- [x] `harrier doctor --integrity` exits 1 against a copy of a database with a
      deliberately damaged page and 0 against a good one (synthetic fixture,
      built in the test); with the container owning the file it exits 75 and
      prints the `docker exec` form:
      `test_integrity_exits_1_on_a_damaged_page_and_0_on_a_good_database`,
      `test_integrity_does_not_open_the_file_the_container_owns`
- [x] `just gate` passes with the `harrier` container running, with no change
      to `services/api/tests/conftest.py`
- [x] spec 051 carries the supersession note, `db.py`'s comment no longer
      claims WAL is safe across the mount, ADR-011 exists with status
      proposed, and the README names `harrier doctor`
- [x] each test above fails with its behavior removed: checked by removing
      each behavior in turn (16 mutants, all failed a test). One check has no
      test of its own; see "What the implementation decided"
- [ ] by hand, on the daily driver: with the container up, `harrier export`
      and `harrier shortlist <id>` from the host exit 75; after `docker
      compose stop harrier` both run; `harrier doctor --integrity` then
      reports `ok`. The pull request records each command, its exit code and
      the verdict line only. No paths, no job output (the repository is
      public; spec 046 records pull request bodies as a past leak)
- [ ] all gates green on the pull request

## What the implementation decided

Recorded here so the spec and the code agree.

- **Logging setup** (amended above, in Behavior).
- **The engine is asked over its unix socket,** not through the `docker`
  binary: `DOCKER_HOST` when it names a unix socket, then
  `/var/run/docker.sock`, then Docker Desktop's `~/.docker/run/docker.sock`.
  No subprocess, and no dependence on launchd's `PATH`. The bound is 3
  seconds. `services/api/src/harrier/container.py`.
- **Only paths under this checkout's `data/` ask the engine** (`live_data_root`
  in `db.py`). The compose file mounts `./data` of the checkout it runs from,
  so nothing else can be the live file. This keeps every test database, demo
  directory and extracted backup away from Docker, which is how the suite
  stays independent of it.
- **Inside the container,** the engine is unreachable and opens go through
  with no special case, as Behavior says. Verified against the compose file;
  the container's own behaviour is unchanged.
- **Engine failure reasons are fixed phrases,** never an exception's text,
  which would carry the socket path and a home directory into `harrier
  doctor`.
- **`create_backup`'s own check has no test that isolates it.** The
  snapshot's check refuses first, and nothing is copied before the snapshot,
  so removing it changes no observable result. It stays as the backstop the
  spec asks for, should the order inside `create_backup` change.
- **`harrier doctor --integrity` opens with `sqlite3` directly,** after the
  ownership check and an existence check, not with `mode=ro`: a read-only open
  of a WAL database fails when the `-shm` file is absent, which is the normal
  state after the container stops. The logic is in
  `services/api/src/harrier/doctor.py`; the CLI only prints it.
- **Two test files outside the new one changed.** The `real_directory`
  fixture in `services/api/tests/test_test_isolation.py` points at the
  operator's data directory on purpose, so it now stubs the engine as
  unreachable; otherwise those tests would depend on whether the container
  happens to run. The docstring of
  `services/api/tests/test_concurrent_writers.py` no longer implies the bind
  mount is safe.
- **Against the real engine,** with the container running on the daily
  driver, `check_database_ownership(default_db_path())` raises
  `DatabaseOwnedByContainer` without opening the file. The by-hand criterion
  below is still the operator's.

## Data and privacy

`harrier doctor` prints `this repository` or `elsewhere` for the mount source,
never the path. Refusal messages name the subcommand, never an argument
value. Tests build synthetic databases in `tmp_path`.

## Honest limitations

- **Until spec 074 ships, the host is refused, not served.** Every host
  command that opens the database exits 75 while the container runs. That
  includes the launchd schedule: reinstalling it before spec 074 means its
  runs are refused, loudly, whenever the container is up.
- **Until spec 075 ships, one window stays open.** A host process that
  passed the check and opened the file before the container started keeps
  its connection while the container opens the same file. `just
  container-up` during a host run reaches it.
- **The cross-kernel behaviour is not pinned by CI.** The Python job has no
  container runtime, which is spec 051's limitation too. The real boundary is
  covered only by the by-hand criterion.
- **The guard covers harrier's own code only.** A raw `sqlite3` shell, a
  database browser, or a script calling `sqlite3.connect` on the path
  bypasses it. `harrier doctor --require-host-access` is a check the operator
  has to choose to run, and it has a gap between the check and the open.
- **Root cause is inferred, not reproduced.** The evidence is SQLite's
  documented requirement, the topology, the overlap in time, and the damaged
  file. Which host access caused it is unknown.
- **`sqlite3 .recover` is best effort.** Whether the rebuilt database lost
  rows on 2026-09-18 is outside what this spec can establish.

## Proof / origin

- The incident: the corrupt copy kept locally under `data/`, and the
  `integrity_check` output summarised in Problem.
- The topology: `docker-compose.yml` (volumes; no Docker socket mounted),
  `docs/adr/ADR-010-container-daily-driver.md`,
  `specs/051-container-daily-driver.md`.
- The WAL setting and the comment to correct: `services/api/src/harrier/db.py:60`.
- The unguarded opens: `services/api/src/harrier/backup.py:102` and `:121`,
  `services/api/src/harrier/logsetup.py`, and the 26 `connect()` calls in
  `services/api/src/harrier_cli/main.py`, all as of commit e8e0c49.
- The test that proved one kernel and not two:
  `services/api/tests/test_concurrent_writers.py`.
- SQLite's requirement: https://sqlite.org/wal.html and
  https://sqlite.org/howtocorrupt.html.
- The 2026-09-18 draft, reviewed with Akin on 2026-10-04 and split into
  this spec, 074 and 075.

## Out of scope

- **Delegation into the container.** Spec 074.
- **The host lease, the 503 on database routes, the `/health` hold fields
  and the web badge.** Spec 075.
- **Option A**, in whole or for single commands.
- **Changing the journal mode.** WAL stays. Under one kernel it is the right
  mode.
- **Auditing or repairing the rebuilt database.** `doctor --integrity`
  reports. It repairs nothing.
- **ADR-010 names the file `data/harrier.db`.** The file is `data/tracker.db`
  (`db.py:16`). Fixed in its own change.
- **Linux or Windows hosts.** On a Linux host the container shares the host
  kernel and the premise differs. macOS only, per ADR-006.

## Migration

For the one operator:

1. After it ships: `harrier doctor` should exit 0, and `harrier doctor
   --integrity` (or its `docker exec` form while the container runs) should
   say `ok`.
2. Keep the launchd schedule uninstalled until spec 074 ships, or accept
   that its runs are refused while the container is up.
3. No schema change. No data change. No container rebuild: the container's
   behaviour does not change.
