---
spec: 060
title: The test suite cannot open the operator's data directory
status: accepted
approved: yes
milestone: M8
depends: [004, 029, 045]
---

# Spec 060: The test suite cannot open the operator's data directory

## Problem

`harrier.db.data_dir()` resolves to `repo_root()/data` unless
`HARRIER_DATA_DIR` is set (or demo mode is on). That is correct for the
operator and wrong for a test, and nothing makes a test choose.

Isolation today is opt-in, per test. Most test files set
`HARRIER_DATA_DIR` in their own fixtures. A test that forgets, or a test
in a file whose fixture it does not request, runs against the real
directory. Two code paths make that expensive:

- `harrier.logsetup.configure_logging` opens a rotating file handler at
  `data_dir()/logs/harrier.log`.
- `harrier.logsetup._install_identity_redaction` calls `connect()` with
  no argument, which opens `data_dir()/tracker.db`, switches it to WAL
  mode, and reads the identity values.

`create_app` calls `configure_logging` (spec 045), so any test that
builds the app without the override does both.

This is observed, not theoretical. On 2026-09-18 the operator's
`data/logs/harrier.log` and its rotations held 8314 lines containing
`testserver`, the hostname only Starlette's test client uses. A test
that logged there had configured logging against the real directory,
and the same call opens the operator's `tracker.db`.

On 2026-09-18 that host-side access coincided with the API container
writing the same database through a Docker bind mount (spec 051), and
the database was corrupted. SQLite's WAL mode relies on shared memory
that does not work across that boundary. The test suite was one of the
host-side processes opening the file.

There is also a privacy cost: a test that opens the real database reads
the operator's candidate and contact identity values into a test
process, which the privacy rule exists to prevent.

No spec states that tests never touch the operator's data. Spec 004
names the override, spec 021 keeps demo runs out of the clone, and the
fresh-clone readiness lens asks the question, but nothing enforces it.

## Scope

- `services/api/tests/conftest.py`: the default data directory and the
  guard.
- `services/api/tests/test_test_isolation.py` (new): the tests that
  prove the guard.
- `docs/architecture.md` or the testing section that describes test
  setup, if one exists: one paragraph naming the rule and the file that
  enforces it.

No change to `harrier.db`, `harrier.logsetup`, or any other source
module. No contract, tracker schema, classification, or web change. The
new test file sits under `services/api/tests/`, which is already
classified public.

## Behavior

**Default data directory.** An autouse fixture in
`services/api/tests/conftest.py` sets `HARRIER_DATA_DIR` to a directory
under the test's own `tmp_path` before the test body runs. Every test
therefore starts with `data_dir()` pointing at an empty, per-test
directory, whether or not it asks.

A test that sets `HARRIER_DATA_DIR` itself still wins: its
`monkeypatch.setenv` runs after the autouse fixture and overrides it.
Existing tests keep their own lines; removing them is not part of this
change.

A test that deletes the variable on purpose
(`services/api/tests/test_classification_coverage.py::test_data_dir_is_inside_the_repository_whatever_the_working_directory`,
`services/api/tests/test_demo.py::test_demo_writes_nothing_into_the_clone`) still can.
Both only compute the path and never open it, so they stay legal under
the guard below.

Subprocesses started by a test inherit the variable through the
environment, so a script or CLI run as a child process resolves the
same temporary directory.

**Guard.** `conftest.py` installs a Python audit hook
(`sys.addaudithook`) once per test session. For the events `open`,
`sqlite3.connect`, and `os.mkdir`, the hook resolves the path argument
and, if it is `repo_root()/data` or anything beneath it, raises
`OperatorDataAccessError`. That error is a plain `Exception` subclass,
not an `OSError` and not a `sqlite3.Error`, so the best-effort handlers
in `logsetup.py` do not swallow it and the test fails with a message
naming the path and the audit event.

The hook raises before the operating system call happens. The guard
therefore prevents the access rather than reporting it afterwards: a
test that slips past the fixture fails without having opened the file.

The guard covers reads as well as writes. Opening `tracker.db` for
reading still creates `-wal` and `-shm` files in WAL mode, and reading
the real log or database pulls operator data into a test.

Paths outside `repo_root()/data` are untouched, including
`config/`, `templates/`, and test fixtures.

## Failure modes

- **A test forgets the override**: it gets the per-test temporary
  directory and passes or fails on its own merits. Nothing reaches the
  real directory.
- **A test deletes the override and then opens the database or
  configures logging**: the audit hook raises
  `OperatorDataAccessError` naming the path. The test fails. The real
  file is not opened.
- **A test passes an explicit path under `repo_root()/data` to
  `connect(path)` or `open`**: same failure. The guard is on the path,
  not on how it was computed.
- **`repo_root()/data` does not exist** (fresh clone, CI): the guard
  compares resolved paths without requiring existence. Nothing is
  created. The suite behaves the same as on the operator's machine.
- **`repo_root()/data` is a symlink**: both sides are resolved before
  comparison, so access through the link target's real path is caught
  too.
- **A path argument that is not path-like** (a file descriptor integer,
  `:memory:`, a `file:` URI): integers and `:memory:` are ignored. A
  `file:` URI is reduced to its path part before comparison.
- **The hook itself errors on an unexpected argument shape**: it must
  not break unrelated opens. Anything it cannot interpret is ignored,
  and the guard's own tests pin the shapes it does interpret.
- **A subprocess opens the real directory**: not caught by the hook,
  which lives in the pytest process only. The inherited
  `HARRIER_DATA_DIR` is the only protection there. See limitations.
- **Logging configured by an earlier test leaves a file handler open on
  that test's temporary directory**: existing behaviour, unchanged. The
  handler points at a temporary path, never the real one, which is the
  property this spec cares about.

## Acceptance criteria

The new tests live in `services/api/tests/test_test_isolation.py`. They
are described here and named when they exist, because
`services/api/tests/test_spec_structure.py::test_every_test_a_spec_names_actually_exists`
fails on a test symbol that is not yet defined.

- [ ] With `HARRIER_DATA_DIR` unset in the invoking shell, a test that
  requests no fixture sees `data_dir()` resolve to a path under its
  `tmp_path` and not under `repo_root()`; a test pins it.
- [ ] A test that sets `HARRIER_DATA_DIR` itself sees its own value; a
  test pins it.
- [ ] With the variable deleted, `connect()` raises
  `OperatorDataAccessError` and no file under `repo_root()/data` is
  created or modified by the attempt; a test pins it.
- [ ] With the variable deleted, `configure_logging(force=True)` raises
  `OperatorDataAccessError` rather than degrading to "file logging is
  unavailable"; a test pins it.
- [ ] `open(repo_root() / "data" / "probe")` raises
  `OperatorDataAccessError` for both read and write modes, and
  `open` on a `tmp_path` file and on `config/resume-content.example.json`
  does not; a test pins it.
- [ ] Removing the autouse fixture from `conftest.py` makes at least one
  of the tests above fail; removing the audit hook makes at least one
  other fail. Stated in the pull request with the output of both runs.
- [ ] The full suite passes with the guard on. Every test that failed
  when the guard was first switched on is listed in the pull request,
  with the fix applied to it (it was touching the real directory).
- [ ] After a full `uv run pytest` in `services/api`, the count of
  `testserver` lines in the operator's `data/logs/harrier.log*` is the
  same as before the run. Checked by hand once at landing and reported
  in the pull request as a before and after count.
- [ ] `just gate` passes.

## Limitations

- The audit hook sees only the pytest process. A child process that
  ignores or unsets `HARRIER_DATA_DIR` can still open the real
  directory. There are 41 `subprocess` call sites in the tests today;
  this spec does not audit them one by one.
- The hook covers `open`, `sqlite3.connect`, and `os.mkdir`. It does
  not cover `os.remove`, `os.rename`, `shutil` operations, or
  `os.scandir`. Those cannot corrupt the database through WAL, which is
  the failure that motivated this, but they are not blocked.
- Audit hooks cannot be removed once installed, so the guard is on for
  the whole session by design. A test cannot opt out.
- This does not explain or repair the 2026-09-18 corruption. It removes
  one of the host-side processes that opens the file. The operator rule
  (stop the container before host-side writes) still stands.

## Proof / origin

- Path resolution: `services/api/src/harrier/db.py`, `data_dir` and
  `connect`.
- The two opens: `services/api/src/harrier/logsetup.py`,
  `configure_logging` and `_install_identity_redaction`.
- `create_app` configures logging: spec 045,
  `services/api/tests/test_logging.py::test_the_api_configures_logging_when_the_app_is_created`.
- Evidence of leakage: `testserver` lines in the operator's local
  `data/logs/harrier.log*` (never in git; the count is a local
  observation on 2026-09-18).
- Container bind mount: spec 051.

## Out of scope

- Changing `data_dir()`, `connect()`, or `logsetup` so production code
  refuses to run under pytest. The source modules stay unaware of the
  test runner.
- Removing the now redundant `monkeypatch.setenv("HARRIER_DATA_DIR",
  ...)` lines from existing tests. That is a refactor and belongs in
  its own change.
- Guarding the home directory, `~/job-hunt-local`, or
  `~/Library/LaunchAgents`. Worth doing; not this spec.
- Guarding `apps/web` tests, which have no access to the Python data
  directory.
- A host and container locking scheme for `tracker.db`, or moving off
  WAL over the bind mount. That is the real fix for the corruption and
  needs its own spec against spec 051.
- Cleaning the existing test lines out of the operator's log.

## Migration

None for users. Contributors: a new test that needs the data directory
gets a temporary one without asking. A test that genuinely needs to
compute the real path may do so but cannot open it.
