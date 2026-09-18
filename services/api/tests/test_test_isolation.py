"""The test suite cannot open the operator's data directory (spec 060).

Isolation used to be opt-in, and the operator's log held lines from
Starlette's test client to show for it. A test that logged there had also
opened the operator's `tracker.db` in WAL mode.

Every test here that aims at the real directory first proves the guard is
live. Without that, deleting the audit hook would turn these tests into the
access they exist to prevent: they would fail, but only after opening the
file.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import (
    OPERATOR_DATA_DIR,
    OPERATOR_DATA_DIRS,
    SESSION_DATA_DIR,
    OperatorDataAccessError,
    guard_is_live,
)

from harrier import logsetup
from harrier.db import connect, data_dir
from harrier.logsetup import configure_logging
from harrier.paths import repo_root

PROBE = OPERATOR_DATA_DIR / "spec-060-isolation-probe"


@pytest.fixture()
def real_directory(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point `data_dir()` at the operator's directory, guard permitting.

    Yields the real path, and afterwards checks the attempt created nothing.
    Modification is not asserted: the API container may be writing the same
    directory while the suite runs, so an mtime comparison would be a coin
    toss. What holds instead is the mechanism, that the hook raises before the
    system call is made.
    """
    assert guard_is_live(), "the operator data guard is not installed; refusing to continue"
    existed = OPERATOR_DATA_DIR.exists()
    monkeypatch.delenv("HARRIER_DATA_DIR", raising=False)
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    assert Path(os.path.realpath(data_dir())) == OPERATOR_DATA_DIR

    yield OPERATOR_DATA_DIR

    assert OPERATOR_DATA_DIR.exists() == existed, "the attempt created the data directory"
    assert not PROBE.exists(), "the attempt created a file in the operator's data directory"


@pytest.fixture()
def restored_logging() -> Iterator[None]:
    """`configure_logging` strips the root handlers before it opens anything."""
    root = logging.getLogger()
    handlers, level, configured = list(root.handlers), root.level, logsetup._configured  # pyright: ignore[reportPrivateUsage]
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)
    logsetup._configured = configured  # pyright: ignore[reportPrivateUsage]


def test_the_default_data_directory_is_temporary(tmp_path: Path) -> None:
    """No fixture of its own, and no help from the invoking shell."""
    resolved = data_dir().resolve()

    assert resolved.is_relative_to(tmp_path.resolve())
    assert not resolved.is_relative_to(repo_root())


def test_nothing_imported_during_collection_logs_to_the_operator() -> None:
    """`harrier_api.app` builds its app at import, which configures logging.

    Collection happens before any fixture, so this is the session default's
    job. It is the leak that actually filled the operator's log: the handler
    opened at import stayed for the whole session.
    """
    import harrier_api.app  # noqa: F401  # pyright: ignore[reportUnusedImport]

    assert not SESSION_DATA_DIR.is_relative_to(repo_root())
    files = [
        Path(os.path.realpath(handler.baseFilename))
        for handler in logging.getLogger().handlers
        if isinstance(handler, logging.FileHandler)
    ]
    for file in files:
        assert not any(file.is_relative_to(guarded) for guarded in OPERATOR_DATA_DIRS), file


def test_a_test_can_still_choose_its_own_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = tmp_path / "chosen"
    monkeypatch.setenv("HARRIER_DATA_DIR", str(chosen))

    assert data_dir() == chosen
    connect().close()
    assert (chosen / "tracker.db").is_file()


def test_a_child_process_inherits_the_temporary_directory(tmp_path: Path) -> None:
    """The hook cannot see a subprocess, so the environment has to carry it."""
    assert Path(os.environ["HARRIER_DATA_DIR"]).is_relative_to(tmp_path)


def test_opening_the_real_database_fails_the_test(real_directory: Path) -> None:
    with pytest.raises(OperatorDataAccessError, match="operator's data directory"):
        connect()


def test_an_explicit_path_into_the_real_directory_fails_too(real_directory: Path) -> None:
    """The guard is on the path, not on how the path was computed."""
    with pytest.raises(OperatorDataAccessError):
        connect(PROBE)
    with pytest.raises(OperatorDataAccessError):
        sqlite3.connect(PROBE)
    with pytest.raises(OperatorDataAccessError):
        sqlite3.connect(f"file:{PROBE}?mode=ro", uri=True)


def test_logging_to_the_real_directory_fails_the_test(
    real_directory: Path, restored_logging: None
) -> None:
    """Not degraded to "file logging is unavailable", which is what an
    `OSError` would have become and which nobody would have read."""
    with pytest.raises(OperatorDataAccessError):
        configure_logging(force=True)


def test_the_guard_is_scoped_to_the_data_directory(real_directory: Path, tmp_path: Path) -> None:
    with pytest.raises(OperatorDataAccessError):
        open(PROBE, encoding="utf-8")  # noqa: SIM115
    with pytest.raises(OperatorDataAccessError):
        open(PROBE, "w", encoding="utf-8")  # noqa: SIM115
    with pytest.raises(OperatorDataAccessError):
        (real_directory / "spec-060-probe-dir").mkdir()

    elsewhere = tmp_path / "plain.txt"
    elsewhere.write_text("fine", encoding="utf-8")
    assert elsewhere.read_text(encoding="utf-8") == "fine"
    example = repo_root() / "config" / "resume-content.example.json"
    assert example.read_text(encoding="utf-8")


def test_a_sibling_whose_name_starts_the_same_is_not_guarded(tmp_path: Path) -> None:
    """`data-export` is not inside `data`. A string prefix would say it was."""
    from conftest import _guarded_path, _operator_data_guard  # pyright: ignore[reportPrivateUsage]

    sibling = OPERATOR_DATA_DIR.with_name("data-export") / "file"
    assert _guarded_path("open", (str(sibling),)) == sibling
    _operator_data_guard("open", (str(sibling), "r", 0))


def test_arguments_that_name_no_path_are_ignored() -> None:
    from conftest import _guarded_path  # pyright: ignore[reportPrivateUsage]

    assert _guarded_path("open", (3, "r", 0)) is None
    assert _guarded_path("sqlite3.connect", (":memory:",)) is None
    assert _guarded_path("open", ()) is None
    assert _guarded_path("open", (object(),)) is None
    sqlite3.connect(":memory:").close()
