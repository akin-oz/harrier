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
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlsplit

import pytest

from harrier.paths import repo_root

TEST_TOKEN = "test-token-not-a-secret"

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


@pytest.fixture(autouse=True)
def _api_token(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    monkeypatch.setenv("HARRIER_API_TOKEN", TEST_TOKEN)


def auth() -> dict[str, str]:
    """Headers a request from the operator's own browser would carry."""
    return {"X-Harrier-Token": TEST_TOKEN}
