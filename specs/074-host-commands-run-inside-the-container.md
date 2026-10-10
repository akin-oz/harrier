---
spec: 074
title: Host commands run inside the container while it owns the database
status: in-progress
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

Every automated test uses a fake detector and a fake runner; the runner is
autouse in both test files, so no test can reach a real container. Tests in
`services/api/tests/test_delegation.py` unless named otherwise.

- [x] every subcommand in `build_parser` has exactly one class, proven by a
      test that walks the parser, so a new command cannot land unclassified:
      `test_every_subcommand_has_exactly_one_class` (it also fails on a path
      option whose destination does not exist);
      `test_a_command_is_classed_by_its_arguments`
- [x] a `database` command with the container owning the database produces a
      `docker exec` of the identical argument vector, as a vector, without a
      TTY, and returns the child's exit code, stdout and stderr:
      `test_a_database_command_is_run_inside_the_container_as_the_same_vector`
- [x] a delegated invocation never opens the database on the host, including
      through logging setup, proven by a test that fails on any
      `sqlite3.connect`:
      `test_a_delegated_command_never_opens_the_database_here`
- [x] a delegating or refusing invocation adds no file handler for
      `data/logs/harrier.log` and leaves that file's size unchanged:
      `test_a_delegating_or_refusing_command_writes_no_log`
- [x] `discover --dataset-file <path>` with the container running exits 75
      before doing any work; `discover --scheduled` is delegated:
      `test_a_command_naming_a_host_file_is_refused_before_any_work`
- [x] a `host-path` invocation with the engine unreachable runs on the host:
      `test_a_host_path_command_runs_here_when_there_is_no_engine`;
      a `host-only` one runs on the host while the container runs:
      `test_a_host_only_command_runs_here_while_the_container_runs`
- [x] delegated `gmail-watch` refreshes the token on the host first; a test
      asserts the refresh precedes the exec and opens no database; a failed
      refresh means no exec:
      `test_gmail_watch_refreshes_the_token_here_before_it_is_handed_over`,
      `test_a_failed_token_refresh_hands_nothing_over`
- [x] a delegated run whose container stops mid-run reports the interruption
      and exits non-zero, with no host retry:
      `test_a_run_whose_container_stops_is_reported_and_not_retried_here`
- [x] the revision mismatch line appears when the fake container's revision
      differs, and not when it matches:
      `test_a_stale_image_is_named_on_the_first_line`; the revision is read
      from the engine's answer:
      `services/api/tests/test_database_ownership.py::test_the_detector_reads_the_image_revision`
- [x] `harrier doctor --integrity` with the container owning the database is
      delegated: `test_doctor_integrity_is_handed_over`
- [x] `harrier schedule install` renders byte-identical plists before and
      after this change: `services/api/src/harrier/schedule.py` and
      `config/schedule.json` are unchanged by it, so the rendering is
      unchanged by construction
- [x] each test above fails with its behavior removed: checked by removing
      each behavior in turn (15 mutants, all failed a test; 2 more for the review fixes on PR #112)
- [ ] by hand, on the daily driver: with the container up, `harrier
      gmail-watch` and `harrier shortlist <id>` from the host run inside the
      container; `harrier export` exits 75; after `docker compose stop
      harrier` all three run on the host. The pull request records each
      command, its exit code, and whether it was delegated. No paths, no job
      or mail output. gmail-watch needs the token moved first (Migration)
- [x] all gates green on the pull request

## What the implementation decided

Recorded here so the spec and the code agree.

- **`docker exec`, through the binary.** Not the engine's exec API over the
  socket, which multiplexes stdout and stderr into one framed stream and
  needs its own stdin plumbing. The binary passes the exit status through
  and forwards stdin with `-i` (`config set` reads it). It is resolved without
  PATH: `HARRIER_DOCKER_BIN`, then PATH, then Docker Desktop's and Homebrew's
  install paths. Without it the command exits 1 and nothing runs on the host
  (`test_without_a_docker_binary_nothing_runs_here_either`).
  `services/api/src/harrier/delegate.py`.
- **A binary that cannot start is reported, not raised** (review finding on
  PR #112). A wrong `HARRIER_DOCKER_BIN` made `subprocess.run` raise out of the
  CLI as a traceback; it now exits 1 with the reason, never the path.
  `test_a_docker_binary_that_cannot_start_is_reported_not_raised`.
- **"Container stopped" is claimed only when known** (same review). The engine
  answering "not running", or no engine at all, means the container stopped.
  An engine that did not answer says nothing, and the command may have
  failed for its own reasons, so nothing is claimed.
  `test_a_stopped_container_is_claimed_only_when_the_engine_says_so`.
- **Corrections to the class list,** from walking the parser:
  `demo-run` is `host-only`, not `database`: it never opens the database, and
  this spec's own failure modes say demo mode is never delegated.
  `brief set --file` and `outreach-draft --input-file` name host files and
  make their commands `host-path`; the first list missed them. `restore` is
  always `host-path` (its archive is a host path). `check` is `database`.
- **Spec 061's tests changed with the behaviour.** A database command
  decided while the container is up is now delegated, so spec 061's refusal
  test drives the race in step 4 instead (container down at the decision, up
  at the open), which is where that refusal is still reached from the CLI.
  Its nested-name test uses a `host-path` command, and its integrity test
  asserts delegation. All three still prove the host never opens the file.
- **The host-path refusal names neither the path nor `docker exec`.** The
  container cannot see the file, so the `docker exec` form would not work;
  the message says to stop the container or use the web app.
- **The revision comparison** uses the form `just container-up` stamps,
  `<short sha>` plus `-dirty`, read from the container's environment in the
  engine's inspect answer, with no extra `docker exec`.
- **The gmail token must be in this checkout's `secrets/`.** Found at
  implementation: the operator's `GMAIL_OAUTH_TOKEN_FILE` pointed outside the
  checkout, so the container, which shares `.env`, received a host path it
  cannot see. With the relative path `.env.example` documents,
  `secrets/google-oauth-token.json`, the host (launchd runs from the repo
  root) and the container (which runs from `/app`) resolve the same file
  through the mount. No code change; a Migration step.

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

1. Put the Gmail OAuth token in this checkout's `secrets/` and set
   `GMAIL_OAUTH_TOKEN_FILE=secrets/google-oauth-token.json` in `.env`, as
   `.env.example` documents. A token anywhere else is unreachable from the
   container, and delegated gmail-watch fails with "missing Gmail OAuth token
   file". Copy it; do not move it out of a directory another system still
   reads from.
2. `just container-up`, so the image carries the current CLI.
3. `harrier schedule install`, if the schedule is to run. The plists are
   unchanged.
4. No schema change. No data change.
