# ADR-011: One kernel opens the tracker database at a time

- Status: proposed
- Date: 2026-10-04
- Supersedes: the database line in ADR-010's Consequences
- Preserves: ADR-010's decision (the container owns the interactive surface,
  launchd owns the cadence), ADR-003 (one write path)

## Context

ADR-010 put the API and the web app in a container and kept the schedule on
the host. Its Consequences named the risk: "Two processes now reach
`data/harrier.db`: the container and the launchd host CLI ... concurrency
across a bind mount is a new question." (The file is `data/tracker.db`.)
Spec 051 answered it with "WAL plus a busy timeout" and proved two writers
under one kernel, with a stated limitation that the proof did not cover the
bind mount.

On 2026-09-18 the database was corrupted while the container and host
processes both had it open. SQLite's WAL mode coordinates processes through
shared memory, and its documentation says every process using a WAL database
must be on the same host (https://sqlite.org/wal.html). The container runs
under the Docker Desktop Linux VM; the host runs macOS. They share the file
through the bind mount and do not share memory. Nothing refused the access;
every process exited 0.

## Options

### The host CLI becomes a client of the container's API

One process owns the file. It needs an endpoint, a contract entry and a client
for each of the CLI's database commands, a second implementation of the CLI
that would drift, and the direct path stays for when the container is down.
Not taken.

### Drop WAL for a rollback journal

The smallest diff. Rollback mode depends on POSIX locks being honoured across
the bind mount, which nobody has verified, and the failure mode stays
corruption. Not taken.

### One kernel at a time: the host refuses, later delegates (chosen)

While the container runs, the database is the container's. A host process that
would open it is refused. A later change runs host commands inside the
container instead of refusing them.

## Decision

At any moment, processes under exactly one kernel have `data/tracker.db` open.
While the `harrier` container runs with this checkout's `data/` mounted, that
kernel is the container's; otherwise it is the host's.

Every open, copy or replacement of the live file asks first, through one
function in `harrier.db` that asks the Docker engine. An engine that cannot
answer is treated as a container that might be running: the host is refused.
There is no override; the way to get host access is to stop the container.

WAL stays. Under one kernel it is the right mode.

## Consequences

- A host command that opens the database while the container runs exits 75
  (spec 061). Until delegation ships (spec 074), that includes the launchd
  schedule, which therefore stays uninstalled.
- A window remains until the host lease ships (spec 075): a host process that
  opened the file before the container started keeps its connection.
- The rule covers harrier's own code only. A raw `sqlite3` session or a
  database browser bypasses it; `harrier doctor --require-host-access` is the
  check to run first.
- The cross-kernel boundary is not exercised by CI, which has no container
  runtime. It is covered by a by-hand check on the daily driver.

## References

- `specs/061-one-kernel-owns-the-database.md`, `specs/074-host-commands-run-inside-the-container.md`,
  `specs/075-host-lease-holds-the-database.md`
- `docs/adr/ADR-010-container-daily-driver.md`, `specs/051-container-daily-driver.md`
- `services/api/src/harrier/db.py` (`check_database_ownership`),
  `services/api/src/harrier/container.py`, `services/api/tests/test_database_ownership.py`
