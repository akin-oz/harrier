---
spec: 076
title: Every tracker connection closes when its caller is done
status: accepted
approved: yes
milestone: M8
depends: [029, 045, 061]
---

# Spec 076: Every tracker connection closes when its caller is done

## Problem

`harrier.db.connect` returns a `sqlite3.Connection` in WAL mode. Most callers
never close it. On CPython 3.12 a connection sits in a reference cycle with
its own statement cache, so it is not freed when the calling function
returns. It closes at the next garbage collection pass or at interpreter
exit. Closing the last connection to a WAL database checkpoints the WAL into
the main file and deletes `-wal` and `-shm`. So today a database write
happens at a moment nobody chose.

Reproduced on the project interpreter (3.12.12): a function opens a WAL
connection, writes, and returns without closing. After it returns `-wal`
still exists. After `gc.collect()` it is gone.

Found on PR #110: the test
`test_integrity_leaves_a_crashed_writers_wal_and_the_database_untouched` saw
logging setup's checkpoint only after calling `gc.collect()`.

Who it hurts:

- **The API.** `create_app` calls `configure_logging`, which loads the
  identity redaction values with `with connect() as conn:`. A context manager
  on a `sqlite3.Connection` commits or rolls back. It does not close. The
  connection lives on inside the server until some later gc pass, then
  checkpoints the live database mid-request from an unrelated thread.
- **Every host CLI run.** 22 command paths in
  `services/api/src/harrier_cli/main.py` open with `conn = connect()` (or pass
  `connect()` inline) and never close. The connection is still open while the
  command prints, returns its exit code, and the process tears down. Spec 075
  removes the host lease "on exit"; a connection that outlives the code that
  removes the lease holds the file with no lease protecting it.
- **Readers of the code.** `db.py` already says each API request "opens and
  closes its own". Three CLI commands (`check`, `check-feeds`, `reconsider`),
  `harrier_api/deps.py` and `harrier_api/demo.py` close in `finally`. The
  others look the same and are not.

## Scope

- `services/api/src/harrier/logsetup.py`: the connection opened for the
  identity values.
- `services/api/src/harrier_cli/main.py`: every command path that calls
  `connect()` without closing it.
- Tests under `services/api/tests/`: `test_logging.py`, a new
  `test_cli_connections.py`, and the `gc.collect()` in
  `test_database_ownership.py`.

## Behavior

- Every connection returned by `harrier.db.connect` inside
  `services/api/src` is closed by the code that opened it before that code
  returns control to its caller, on success and on exception.
- `configure_logging` closes the connection it opens for the identity values
  before it returns, whether `identity_values` succeeds or raises. Its
  observable logging behavior (handlers, redaction, warnings, the
  ownership refusal path from spec 061) does not change.
- Every `harrier` subcommand that opens the database has closed it by the
  time the subcommand's function returns, including when the command
  returns a non-zero exit code or raises.
- A connection handed out by the FastAPI dependency `get_conn` stays as it
  is: closed in `finally` when the request ends.
- No caller opens a second connection to replace one it closed. The number
  of connections a command opens does not change.

## Failure modes

- **`identity_values` raises `sqlite3.Error`.** The connection is closed,
  then `_load_identity_values` returns `None` as today, and logging warns
  that redaction is unavailable.
- **`connect` itself raises** (missing directory, `DatabaseOwnershipError`).
  There is no connection to close. Behavior is unchanged: ownership errors
  propagate to `configure_logging`, other errors return `None`.
- **A command raises mid-run** (for example `MigrationError` in
  `migrate-legacy`, or an LLM provider error in `tailor`). The connection is
  closed before the exception leaves the command function.
- **A command returns early** (validation failure, "nothing to do"). The
  connection is closed on that path too.
- **A transaction is open at close.** Closing does not commit. Any command
  that relied on the implicit commit-at-gc would lose writes. Today every
  write path commits through the tracker store's own `with conn:` blocks, so
  this is expected to change nothing; the full test suite is the check.
- **A closed connection is used after close.** `sqlite3.ProgrammingError`.
  No caller may use the connection after its close; a test that hits this
  is a real bug, not a test to relax.

## Acceptance criteria

- `services/api/tests/test_logging.py::test_configure_logging_closes_its_connection`:
  a temporary WAL database with no other open connection; `gc.disable()`;
  `configure_logging(force=True)`; afterwards `-wal` and `-shm` do not exist.
  Fails on `main` today (both files exist until `gc.collect()`).
- `services/api/tests/test_logging.py::test_configure_logging_closes_its_connection_when_identity_values_fails`:
  same setup with `identity_values` patched to raise `sqlite3.OperationalError`;
  no `-wal` or `-shm` after the call, and the "identity redaction is
  unavailable" warning is logged.
- `services/api/tests/test_cli_connections.py`: `harrier.db.connect` (as
  imported by `harrier_cli.main`) wrapped to record every connection it
  returns, with gc disabled. For at least one read command (`profile list`),
  one write command (`tracker` verb), and one command that fails mid-run
  (`migrate-legacy` with an input that raises `MigrationError`), every
  recorded connection raises `sqlite3.ProgrammingError` on `execute` after
  `main()` returns. Each case fails on `main` today.
- `test_integrity_leaves_a_crashed_writers_wal_and_the_database_untouched`
  in `services/api/tests/test_database_ownership.py` no longer needs
  `gc.collect()` to observe logging setup's close. The call is removed or
  its comment says why it stays.
- `grep -nE "with connect\(" services/api/src` returns nothing.
- `just check` passes.

## Out of scope

- `sqlite3.connect` calls outside `harrier.db.connect`: `backup.py` and
  `doctor.py` open their own connections and already close them. Not
  touched.
- Test code under `services/api/tests/`. Tests that leak connections are
  not production behavior; fixing them belongs in its own change if it
  matters.
- Changing `harrier.db.connect`'s signature, or adding a context-manager
  helper to `harrier.db`, unless the implementation shows the per-call-site
  fix cannot be made reliably. If it is needed, this spec is amended first.
- Changing the journal mode, the busy timeout, or anything spec 061 or 075
  decides about who may open the file.
- Restructuring CLI command handlers beyond adding the close.

## Migration

None. No data, config or command-line change.

## Proof / origin

- Review on PR #110 (spec 061): the integrity test observed logging setup's
  checkpoint only after `gc.collect()`.
- Reproduced on CPython 3.12.12 in this session: a WAL connection left open
  by a returning function keeps `-wal` until `gc.collect()`.
- Call sites as of commit 2d318d7: `logsetup.py:134` and the `connect()`
  calls in `harrier_cli/main.py` outside `check`, `check-feeds` and
  `reconsider`.

## Proof map

| Claim | Proof |
| --- | --- |
| logging setup closes its connection | `test_logging.py::test_configure_logging_closes_its_connection` |
| it closes on the error path too | `test_logging.py::test_configure_logging_closes_its_connection_when_identity_values_fails` |
| CLI commands close on success and failure | `test_cli_connections.py` |
| no `with connect()` remains | the grep in Acceptance criteria |
