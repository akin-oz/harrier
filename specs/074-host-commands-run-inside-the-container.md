---
spec: 074
title: Host commands run inside the container while it owns the database
status: accepted
approved: yes
milestone: M8
depends: [050, 051, 061, 064]
---

# Spec 074: Host commands run inside the container while it owns the database

Split from the 2026-09-18 draft of spec 061 on 2026-10-04. Spec 061 makes the
host refuse; this spec makes it serve.

## Problem

After spec 061, every host command that opens the database exits 75 while the
`harrier` container runs. That is safe, and for the launchd schedule it is a
dead end: launchd fires on the host (ADR-006, ADR-010), the container is up
nearly all the time, so discovery, the digest and gmail-watch would be
refused on almost every run. The operator wants the schedule back.

Interactive host commands have the same problem at lower cost: `harrier
shortlist <id>` from a terminal is refused and has to be retyped as `docker
exec harrier harrier shortlist <id>`.

## Scope

- `services/api/src/harrier_cli/main.py`: a class for every subcommand, and
  the decision to run, delegate or refuse, made before logging setup.
- A delegation runner (`docker exec`), and the gmail-watch credential refresh
  before delegating.
- `harrier doctor --integrity` delegates instead of printing the `docker
  exec` form (spec 061).
- Tests under `services/api/tests/`.
- README: the operator section says host commands run inside the container
  while it is up.

The guard, the API, the contract, `apps/web` and the plists do not change.

## Behavior

### Classes

Every CLI subcommand carries exactly one class, declared next to the parser:

| Class | Meaning | While the container owns the database |
|---|---|---|
| `database` | opens the live file; its inputs and outputs are the database, `data/`, `config/`, env and network | delegated |
| `host-path` | a `database` command invoked with a path-valued argument, explicit or defaulted | refused, exit 75 |
| `host-only` | never opens the live file | runs on the host unchanged |

Initial assignment, from reading `build_parser`, to be confirmed at
implementation and corrected by amendment:

- `host-only`: `gmail-oauth`, `schedule *`, `review-followup`, `parity *`,
  `doctor` (except `--integrity`), `verify-backup`.
- `host-path` whenever such an argument is present: `migrate-legacy`,
  `profile import`, `profile export`, `export`, `cutover *`,
  `gmail-migrate-state`, `restore`, `config set --file`, `backup --dest`,
  `discover` with `--dataset-file`, `--wellfound-file` or `--wttj-file`, and
  `tailor`, `cover-letter`, `answers`, `evaluate`, `outreach-draft` with
  `--jd-file`, `--notes-file` or `--questions-file`.
- `database`: everything else, including `demo-run`, every `config`
  subcommand other than `set --file`, `backup` without `--dest` (its default
  destination is mounted at `/app/backups`, spec 064), `doctor --integrity`,
  and the three scheduled invocations.

### The decision

The host CLI decides in this order, before logging setup and before anything
opens the database:

1. Parse the arguments and read the subcommand's class. `host-only` runs.
2. Ask the detector (spec 061) who owns the database.
3. Container owns it: a `database` command is delegated; a `host-path`
   command exits 75 with a message saying to stop the container or use the
   web app. Ownership unknown: exit 75, as spec 061.
4. Host may open: configure logging and run as today. If the container starts
   between step 2 and the first open, spec 061's guard refuses the open with
   exit 75. Spec 075 turns that case into a delegation.

### Delegation

- Runs the identical argument vector inside the `harrier` container and
  returns that process's exit code, stdout and stderr.
- Requests no TTY, so it works under launchd.
- Is not a shell: the arguments are passed as a vector, never joined into a
  command string.
- When the container's revision (spec 051 health fields) differs from the
  working tree's, the first line of stderr says so. The run proceeds.

The delegating host process does not configure logging and writes nothing to
`data/logs/harrier.log`. The identity redaction filter needs a database open
(`services/api/src/harrier/logsetup.py`), which this process is not allowed to
make, so a log line from it would be unredacted. Its own output is fixed text
on stderr: the subcommand name, the container name, the container revision,
and the engine error when there is one. The process inside the container
configures its own logging, redaction included.

### gmail-watch and the read-only secrets mount

`load_gmail_credentials` refreshes an expired access token and writes it back
to the token file (`services/api/src/harrier/mail/watch.py:641`). `secrets/` is
read-only in the container by design (spec 050, spec 051), so that write
raises inside the container. Today a host gmail-watch run refreshes the file;
once gmail-watch is delegated, nothing on the host would.

So before delegating `gmail-watch`, the host CLI performs the credential
refresh on the host, where `secrets/` is writable. That step does not open the
database. The mount stays read-only. Spec 051's "fail loudly" rule for a
refresh inside the container stands.

## Failure modes

- **Container stops during a delegated run.** The job dies with it. The host
  CLI reports "delegated run interrupted: container stopped" and exits
  non-zero. It does not retry on the host, because the container may be
  coming back. SQLite's own recovery handles the killed writer, under one
  kernel.
- **`docker exec` fails** (container unhealthy, engine error mid-call): that
  error is the output, and the exit code is non-zero.
- **Engine hangs.** As spec 061: refused with exit 75. No success is recorded,
  so spec 029's last-success age shows the gap, and launchd's next firing
  retries.
- **Stale image.** A delegated command runs the image's code, not the working
  tree's. The mismatch is printed, not prevented.
- **The gmail token refresh fails on the host.** gmail-watch is not
  delegated; the refresh error is the output, with a non-zero exit, as a
  refresh failure is today.
- **Fresh clone, no Docker.** Nothing is ever delegated. CI is this case.
- **Demo mode.** Never delegated (spec 021).

Failure modes this must not introduce:

- A delegated invocation that opens the database on the host, including
  through logging setup.
- An unredacted line in `data/logs/harrier.log` from a delegating or refusing
  host process.
- An argument value in any host-side output other than the child's own.

## Acceptance criteria

Proving symbols are named at implementation. Every automated test uses a fake
detector and a fake runner.

- [ ] every subcommand in `build_parser` has exactly one class, proven by a
      test that walks the parser, so a new command cannot land unclassified
- [ ] a `database` command with the container owning the database produces a
      `docker exec` of the identical argument vector, as a vector, without a
      TTY, and returns the child's exit code, stdout and stderr
- [ ] a delegated invocation never opens the database on the host, including
      through logging setup, proven by a test that fails on any
      `sqlite3.connect`
- [ ] a delegating or refusing invocation adds no file handler for
      `data/logs/harrier.log` and leaves that file's size unchanged
- [ ] `discover --dataset-file <path>` with the container running exits 75
      before doing any work; `discover --scheduled` is delegated
- [ ] a `host-path` invocation with the engine unreachable runs on the host
- [ ] delegated `gmail-watch` refreshes the token on the host first; a test
      asserts the refresh precedes the exec and opens no database; a failed
      refresh means no exec
- [ ] a delegated run whose container stops mid-run reports the interruption
      and exits non-zero, with no host retry
- [ ] the revision mismatch line appears when the fake container's revision
      differs, and not when it matches
- [ ] `harrier doctor --integrity` with the container owning the database is
      delegated
- [ ] `harrier schedule install` renders byte-identical plists before and
      after this change
- [ ] by hand, on the daily driver: with the container up, `harrier
      gmail-watch` and `harrier shortlist <id>` from the host run inside the
      container; `harrier export` exits 75; after `docker compose stop
      harrier` all three run on the host. The pull request records each
      command, its exit code, and whether it was delegated. No paths, no job
      or mail output
- [ ] all gates green on the pull request

## Data and privacy

The delegating process prints the subcommand name, never an argument value.
The child's own output is whatever the command prints today. Tests use
synthetic argument values.

## Honest limitations

- **A scheduled run now depends on the Docker engine answering** while the
  container is up. When it does not, the run is refused, which is loud.
- **Delegated runs use the image's revision.** After a `git pull` without
  `just container-up`, scheduled jobs run older code than the working tree.
- **`host-path` commands are refused while the container runs.** Mapping host
  paths into the mounts is its own spec.
- **The delegating process leaves no line in `harrier.log`.** That a run was
  delegated is visible in the log lines of the process inside the container
  and in launchd's captured stderr.
- **`data/logs/harrier.log` is still appended by both kernels** when a host
  `host-only` command and the container log at once. A torn line is possible.
  It is not a corruption risk.
- **The real `docker exec` boundary is not pinned by CI.** Only the by-hand
  criterion covers it.

## Proof / origin

- The 2026-09-18 draft of spec 061, sections "Delegation" and "gmail-watch
  and the read-only secrets mount", split out on 2026-10-04.
- The schedule: `config/schedule.json`, `services/api/src/harrier/schedule.py`.
- The token write-back: `services/api/src/harrier/mail/watch.py:641`.
- The path arguments: `discover` at `services/api/src/harrier_cli/main.py:1499`
  as of commit e8e0c49.
- The backup destination mount: spec 064.

## Out of scope

- **The host lease and the window it closes.** Spec 075.
- **Path translation for `host-path` commands.**
- **Moving the schedule into the container.** ADR-006 and ADR-010 stand.
- **Making `secrets/` writable in the container.**
- **The API delegating anything.** It already runs in the container.

## Migration

1. `just container-up`, so the image carries the current CLI.
2. `harrier schedule install`, if the schedule is to run. The plists are
   unchanged.
3. No schema change. No data change.
