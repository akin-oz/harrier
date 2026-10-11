"""Shared test setup.

The API token (spec 035) is fixed here rather than generated per test, so a
test that means to present it can, and a test that means to omit it still
exercises the refusal.

Setting it in the environment also keeps the tests from writing a token file
into a real data directory: `harrier_api.localauth.load_or_create_token`
returns the override before it touches the filesystem.

The data directory (spec 060) is handled the same way and for a harder reason.
Isolation used to be opt-in: a test that forgot `HARRIER_DATA_DIR` ran against
`repo_root()/data`, and any test that built the app configured logging there,
which opens the operator's `tracker.db` in WAL mode and appends to the
operator's log. Two layers now: the suite runs against temporary data
directories, and an audit hook fails anything that reaches a real one anyway.

The temporary directory is set twice, and the first time matters most.
`harrier_api.app` builds its app at import, so eleven test files configured
logging during collection, before any fixture had run. That handler then stayed
on the operator's log for the whole session, because `configure_logging` is
idempotent. A fixture alone could not have fixed it, so the session default is
set here at import, which pytest guarantees happens before it imports a test
module in this directory.

The test client's host (spec 083) is set at import for the same reason.
Starlette's `TestClient` sends Host `testserver` unless told otherwise, and the
API no longer trusts that name, so the client's default becomes `localhost`
before any test module builds one.
"""

from __future__ import annotations

import atexit
import fcntl
import inspect
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlsplit

import pytest
from fastapi.testclient import TestClient
from starlette.types import ASGIApp

from harrier.paths import repo_root

TEST_TOKEN = "test-token-not-a-secret"

# --- test client host (spec 083) ---

# Replaced at import, the way the data directory is below, so every client the
# suite builds reaches the app on a host it trusts without each construction
# site naming one, and a new test cannot forget it. A test that passes its own
# base URL still gets it.
TEST_CLIENT_BASE_URL = "http://localhost"
# Pyright strict cannot resolve the httpx cookie type in this signature, the
# same gap every API test file switches off for the whole file.
_starlette_test_client_init = TestClient.__init__  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]


def _init_on_a_trusted_host(
    self: TestClient,
    app: ASGIApp,
    base_url: str = TEST_CLIENT_BASE_URL,
    *args: Any,
    **kwargs: Any,
) -> None:
    _starlette_test_client_init(self, app, base_url, *args, **kwargs)


TestClient.__init__ = _init_on_a_trusted_host

# --- operator data guard (spec 060) ---

# Resolved once, so a symlinked data directory is guarded at its real path and
# a test that patches `repo_root` cannot move the fence.
OPERATOR_DATA_DIR = Path(os.path.realpath(repo_root() / "data"))


def _operator_data_dirs() -> tuple[Path, ...]:
    """Every directory that is the operator's, read before the override below.

    A shell that exports `HARRIER_DATA_DIR` (the container does) has named its
    real data directory there, and it is no more a test's to open than
    `repo_root()/data` is.
    """
    inherited = os.environ.get("HARRIER_DATA_DIR", "").strip()
    if not inherited:
        return (OPERATOR_DATA_DIR,)
    return (OPERATOR_DATA_DIR, Path(os.path.realpath(inherited)))


OPERATOR_DATA_DIRS = _operator_data_dirs()

# The session default. Set at import so it is in place before pytest imports
# the first test module, and unconditionally so an inherited value cannot win.
SESSION_DATA_DIR = Path(tempfile.mkdtemp(prefix="harrier-tests-")) / "data"
os.environ["HARRIER_DATA_DIR"] = str(SESSION_DATA_DIR)
# The local store, whatever the operator's shell or `.env` names. An exported
# empty value wins over `.env` (spec 011), so a hosted URL there never reaches
# a test, which sets its own when it wants Postgres (spec 060, spec 103).
os.environ["HARRIER_DATABASE_URL"] = ""
atexit.register(shutil.rmtree, SESSION_DATA_DIR.parent, ignore_errors=True)

GUARD_PROBE_EVENT = "harrier.tests.operator_data_guard_probe"

# `sqlite3.connect` is listed because sqlite opens its file in C, which never
# raises the `open` event. `os.mkdir` is listed because `connect()` and the
# log handler both create their parent directory before opening anything.
_GUARDED_EVENTS = frozenset({"open", "sqlite3.connect", "os.mkdir"})


class OperatorDataAccessError(Exception):
    """A test reached for the operator's real data directory.

    Deliberately not an `OSError` and not a `sqlite3.Error`: `logsetup` treats
    both as "log storage is unavailable" and carries on, which would turn this
    into a warning nobody reads.
    """


def _descriptor_path(fd: int) -> str | None:
    """The directory a descriptor refers to, where the platform can say."""
    proc = f"/proc/self/fd/{fd}"
    if os.path.exists(proc):
        return os.readlink(proc)
    get_path = getattr(fcntl, "F_GETPATH", None)
    if get_path is None:
        return None
    return os.fsdecode(fcntl.fcntl(fd, get_path, b"\0" * 1024).rstrip(b"\0"))


def _guarded_path(event: str, args: tuple[Any, ...]) -> Path | None:
    """The filesystem path an audited call names, or None if it names none."""
    if not args:
        return None
    raw = args[0]
    # A file descriptor. `bool` is an int, and neither is a path.
    if isinstance(raw, int):
        return None
    if isinstance(raw, os.PathLike):
        raw = os.fspath(cast("os.PathLike[str]", raw))
    if not isinstance(raw, (str, bytes)):
        return None
    text = os.fsdecode(raw)
    if event == "sqlite3.connect":
        if text == ":memory:" or not text:
            return None
        if text.startswith("file:"):
            text = unquote(urlsplit(text).path)
            if not text:
                return None
    # `os.mkdir` reports its `dir_fd`, so a relative path can be resolved
    # against the directory it is really relative to. `open` reports none, which
    # is a limitation the spec states rather than one this can close.
    if event == "os.mkdir" and len(args) > 2 and not os.path.isabs(text):
        dir_fd = args[2]
        if isinstance(dir_fd, int) and not isinstance(dir_fd, bool):
            base = _descriptor_path(dir_fd)
            if base is None:
                return None
            text = os.path.join(base, text)
    return Path(os.path.realpath(text))


def _operator_data_guard(event: str, args: tuple[Any, ...]) -> None:
    if event == GUARD_PROBE_EVENT:
        # Lets a test prove the hook is live before it aims at the real
        # directory. Without this, deleting the hook would turn the guard's own
        # tests into the access they exist to prevent.
        args[0].append(True)
        return
    if event not in _GUARDED_EVENTS:
        return
    try:
        path = _guarded_path(event, args)
    except Exception:
        # An argument shape this does not understand must not break an
        # unrelated open. The shapes it does understand are pinned by
        # test_test_isolation.py.
        return
    if path is None:
        return
    if any(path == guarded or guarded in path.parents for guarded in OPERATOR_DATA_DIRS):
        raise OperatorDataAccessError(
            f"{event} on {path}: a test reached the operator's data directory. "
            "Leave HARRIER_DATA_DIR at the temporary directory conftest.py sets (spec 060)."
        )


def guard_is_live() -> bool:
    """Whether the audit hook is installed and answering."""
    answered: list[bool] = []
    sys.audit(GUARD_PROBE_EVENT, answered)
    return bool(answered)


# Installed at import, once per session. An audit hook cannot be removed, which
# is the point: no test can opt out.
sys.addaudithook(_operator_data_guard)


@pytest.fixture(autouse=True)
def _data_dir(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every test starts with an empty data directory of its own.

    Narrower than the session default above, which exists for imports and is
    shared. Autouse fixtures run before the ones a test requests, so a test
    that sets `HARRIER_DATA_DIR` itself still gets its own value.
    """
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HARRIER_DATABASE_URL", "")


# --- the store URL pin (spec 103) ---

DOTENV_FALLBACK = "dotenv_fallback"
URL_PIN = "HARRIER_DATABASE_URL"

# pytester runs the net's own test (test_store_cli.py) against this file.
pytest_plugins = ["pytester"]


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{DOTENV_FALLBACK}: the test removes HARRIER_DATABASE_URL to read the .env "
        "fallback, from a working directory under its tmp_path",
    )
    _read_no_dotenv_outside_the_tests_tmp_paths()


def pytest_unconfigure(config: pytest.Config) -> None:
    import harrier.envfile as envfile

    if _real_read_env_file:
        envfile.read_env_file = _real_read_env_file.pop()


# --- the .env files the suite may read (spec 103) ---

# Every `.env` read in this process goes through `harrier.envfile.read_env_file`:
# the CLI's loader and `store_target` both import it when they run, not at
# import time. Replaced for the session, it reads only a file under pytest's
# base temporary directory, where every test's tmp_path is. An operator's
# `.env` beside the working directory is then never read, whatever a test
# does to the pin, including a loss no audit event shows and a test restores
# before it ends (post-merge review of PR #243). A child process does not
# run this file, so this does not reach a child; the net below records those.
_dotenv_root: Path | None = None
_real_read_env_file: list[Callable[[Path], dict[str, str]]] = []


def _read_no_dotenv_outside_the_tests_tmp_paths() -> None:
    import harrier.envfile as envfile

    real = envfile.read_env_file

    def read_env_file(path: Path) -> dict[str, str]:
        # None until the session fixture below runs, so nothing is read
        # during collection.
        root = _dotenv_root
        where = Path(os.path.realpath(path))
        if root is None or (where != root and root not in where.parents):
            return {}
        return real(path)

    _real_read_env_file.append(real)
    envfile.read_env_file = read_env_file


@pytest.fixture(scope="session", autouse=True)
def _dotenv_root_is_the_base_temp(  # pyright: ignore[reportUnusedFunction]
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    global _dotenv_root
    _dotenv_root = Path(os.path.realpath(tmp_path_factory.getbasetemp()))
    try:
        yield
    finally:
        _dotenv_root = None


# What each kind of event did to the pin, said of a test and said alone.
# Every kind but "set" is a moment `.env` could choose the store.
_PIN_EVENTS: dict[str, tuple[str, str]] = {
    "unset": ("unset HARRIER_DATABASE_URL", "unset"),
    "child": ("started a child without HARRIER_DATABASE_URL", "left out of a child's environment"),
    "loader": ("let the .env loader write HARRIER_DATABASE_URL", "written by the .env loader"),
    "gone": (
        "removed HARRIER_DATABASE_URL without an unset event",
        "removed without an unset event",
    ),
}


@dataclass(frozen=True)
class _PinUnset:
    """One event on the pin, recorded as it happened."""

    # A key of _PIN_EVENTS, or "set" for a plain set.
    kind: str
    # Collection, the setup, call or teardown of a test, or between tests.
    phase: str
    # The working directory then, whose `.env` chose the store; for a
    # child, the child's.
    cwd: Path
    # The test whose watch was open, or None outside every test's watch.
    test: str | None


# Every event on the pin in this session, and what was running at the time.
_pin_unsets: list[_PinUnset] = []
_pin_phase = "collection"
_watched_test: str | None = None


def _names_the_pin(name: object) -> bool:
    return isinstance(name, (str, bytes)) and os.fsdecode(name) == URL_PIN


def _from_the_dotenv_loader() -> bool:
    """Whether the CLI's `.env` loader is on the stack. It writes only a
    key that is absent, so its writing the pin means the pin was gone."""
    frame = inspect.currentframe()
    while frame is not None:
        if (
            frame.f_code.co_name == "load_project_env"
            and frame.f_globals.get("__name__") == "harrier_cli.main"
        ):
            return True
        frame = frame.f_back
    return False


def _record(kind: str, cwd: Path) -> None:
    _pin_unsets.append(_PinUnset(kind, _pin_phase, cwd.resolve(), _watched_test))


def _watch_the_pin(event: str, args: tuple[Any, ...]) -> None:
    """Record every event on the pin, whenever it happens.

    Every way code removes a variable (`del os.environ[...]`, `pop`,
    monkeypatch's `delenv`) calls `os.unsetenv`, which raises this event.
    The value at the end of a test cannot show it: once the pin is gone,
    anything that loads `.env`, as the CLI's `main()` does, writes the
    variable back with the URL the file names.

    A child started with an `env` that leaves the pin out loads `.env` from
    its own working directory, and the `.env` loader writing the pin means
    it was gone, by whatever path (privacy review of PR #243).
    """
    if event == "os.unsetenv" and args and _names_the_pin(args[0]):
        _record("unset", Path.cwd())
    elif event == "os.putenv" and args and _names_the_pin(args[0]):
        _record("loader" if _from_the_dotenv_loader() else "set", Path.cwd())
    elif (
        event == "subprocess.Popen"
        and len(args) >= 4
        and args[3] is not None
        and not any(_names_the_pin(name) for name in args[3])
    ):
        cwd = args[2]
        _record("child", Path(os.fsdecode(cwd)) if cwd is not None else Path.cwd())


sys.addaudithook(_watch_the_pin)


@contextmanager
def _in_phase(phase: str) -> Generator[None]:
    global _pin_phase
    _pin_phase = phase
    try:
        yield
    finally:
        _pin_phase = "between tests"


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item: pytest.Item) -> Iterator[None]:
    with _in_phase(f"setup of {item.nodeid}"):
        yield


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Iterator[None]:
    with _in_phase(f"call of {item.nodeid}"):
        yield


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item) -> Iterator[None]:
    with _in_phase(f"teardown of {item.nodeid}"):
        yield


@pytest.fixture(autouse=True)
def _database_url_stays_pinned(  # pyright: ignore[reportUnusedFunction]
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    """Fail a test during which HARRIER_DATABASE_URL was ever unset.

    Unset, the store is chosen from `.env` in the working directory, which is
    wherever pytest was started. A fixture that deleted the pin above ran
    SQLite tests against the database an operator's `.env` named (review of
    PR #218). A test marked `dotenv_fallback` may unset it from a working
    directory under its own tmp_path.

    This detects after the fact; it does not prevent. A test that unsets
    the pin has already run against whatever store `.env` named when it
    fails here. Every unset is recorded as it happens, so a test whose
    `.env` wrote the variable back still fails (review of PR #225). A child
    started without the pin, and the `.env` loader writing it, count as an
    unset. This test's watch opens after `_data_dir` sets the pin to an
    empty string. An event before it opens, or after it closes, is outside
    every watch, and the session fails on it below (post-merge review of
    PR #231).

    A path that raises no event, such as deleting from `os.environ._data`
    or replacing `os.environ` with a mapping that lacks the pin, is caught
    at the end of the test: the pin must still be present. If `.env` wrote
    it back meanwhile, the loader's write is the event (a claims audit of
    PR #243). A loss the test restores before it ends is not seen here, but
    while it lasts no `.env` outside the base temporary directory is read
    in this process (above; post-merge review of PR #243). The value is not
    required to be empty: a Postgres test sets its own URL through
    monkeypatch.

    Checked after the test and before the monkeypatch fixture restores the
    environment: this fixture asks for `monkeypatch`, so it is set up after
    it and torn down before it.
    """
    global _watched_test
    # pytest leaves `node` untyped on FixtureRequest; inside a function
    # fixture it is the test item.
    test = cast("pytest.Item", request.node)  # pyright: ignore[reportUnknownMemberType]
    first = len(_pin_unsets)
    _watched_test = test.nodeid
    try:
        yield
    finally:
        _watched_test = None
    events = _pin_unsets[first:]
    here = Path.cwd().resolve()
    ending = f"teardown of {test.nodeid}"
    if URL_PIN not in os.environ and not any(event.kind == "unset" for event in events):
        events.append(_PinUnset("gone", ending, here, test.nodeid))
    breaches = [event for event in events if event.kind != "set"]
    if not breaches:
        return
    home = tmp_path.resolve()
    if test.get_closest_marker(DOTENV_FALLBACK) is not None:
        outside = [
            event
            for event in breaches
            if event.kind == "gone" or (event.cwd != home and home not in event.cwd.parents)
        ]
        if not outside:
            return
        pytest.fail(
            f"{test.nodeid} {_PIN_EVENTS[outside[0].kind][0]} outside its tmp_path, "
            f"so .env in {outside[0].cwd} chose the store"
        )
    pytest.fail(
        f"{test.nodeid} {_PIN_EVENTS[breaches[0].kind][0]}, so .env in {breaches[0].cwd} chose "
        f"the store, whatever it holds now. Set it to an empty string for SQLite, or mark the "
        f"test {DOTENV_FALLBACK} and move to tmp_path first (spec 103)"
    )


@pytest.fixture(scope="session", autouse=True)
def _database_url_never_unset_outside_a_test() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Fail the session if the pin was unset outside every test's watch.

    A module-scoped fixture, an import during collection, or a fixture's
    teardown runs outside the watch above, and `.env` chooses the store for
    whatever runs next. No test owns that, so the session fails, at the
    teardown of its last test, naming the phase.
    """
    yield
    stray = [event for event in _pin_unsets if event.test is None and event.kind != "set"]
    if stray:
        pytest.fail(
            f"HARRIER_DATABASE_URL was {_PIN_EVENTS[stray[0].kind][1]} outside every test's "
            f"watch, during {stray[0].phase}, so .env in {stray[0].cwd} chose the store for "
            f"what ran then. "
            f"Keep it set in fixtures and at import; a test that needs the .env fallback "
            f"is marked {DOTENV_FALLBACK} and unsets it itself (spec 103)"
        )


@pytest.fixture(autouse=True)
def _no_built_spa(  # pyright: ignore[reportUnusedFunction]
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`create_app` mounts `apps/web/dist` when it exists, and `just check`
    builds it, so a test's routes used to depend on whether the web app had
    been built on that machine: a POST that normalizes to a path no API route
    holds got 405 from the static mount there and 404 in CI. Every test now
    sees no built SPA unless it passes `spa_dir` itself, as test_demo.py's
    SPA tests do (spec 096, review of PR #224)."""
    import harrier_api.app as app_module

    monkeypatch.setattr(app_module, "spa_dist_dir", lambda: tmp_path / "no-built-spa")


@pytest.fixture(autouse=True)
def _api_token(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    monkeypatch.setenv("HARRIER_API_TOKEN", TEST_TOKEN)


def auth() -> dict[str, str]:
    """Headers a request from the operator's own browser would carry."""
    return {"X-Harrier-Token": TEST_TOKEN}
