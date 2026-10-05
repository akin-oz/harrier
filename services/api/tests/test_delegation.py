"""Host commands run inside the container while it owns the database (spec 074).

Every test fakes both halves: `harrier.container.detect` says who owns the
database, and `harrier.delegate.run_process` stands in for `docker exec`. The
fake runner is autouse, so no test here can reach the real container, which
may well be running on the machine executing the suite.
"""

from __future__ import annotations

import argparse
import gc
import logging
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

import pytest

import harrier.mail.watch as watch
from harrier import container, db, delegate, logsetup
from harrier.container import ContainerState
from harrier.db import connect, data_dir
from harrier_cli.main import (
    COMMAND_CLASSES,
    DATABASE,
    HOST_ONLY,
    HOST_PATH,
    build_parser,
    command_class,
    main,
    subcommand_name,
)

UNREACHABLE = ContainerState(engine=container.UNREACHABLE)


@pytest.fixture()
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data directory standing in for the one the container mounts."""
    root = tmp_path / "live-data"
    root.mkdir()
    monkeypatch.setattr(db, "live_data_root", lambda: root)
    monkeypatch.setenv("HARRIER_DATA_DIR", str(root))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    return root


def owned(root: Path, revision: str | None = None) -> ContainerState:
    return ContainerState(
        engine=container.REACHABLE, running=True, data_source=root, revision=revision
    )


@pytest.fixture()
def answers(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Queue what the engine says, in order. The last answer repeats."""

    def install(*states: ContainerState) -> None:
        queue = list(states)

        def detect(timeout: float = container.DEFAULT_TIMEOUT_SECONDS) -> ContainerState:
            return queue.pop(0) if len(queue) > 1 else queue[0]

        monkeypatch.setattr(container, "detect", detect)

    return install


class FakeDocker:
    """Stands in for `docker exec`: records each command, answers `status`."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.status = 0

    def __call__(self, command: Sequence[str]) -> int:
        self.calls.append(list(command))
        return self.status


@pytest.fixture(autouse=True)
def runner(monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    fake = FakeDocker()
    monkeypatch.setattr(delegate, "run_process", fake)
    monkeypatch.setattr(delegate, "docker_binary", lambda: "docker")
    monkeypatch.setattr(delegate, "working_tree_revision", lambda: None)
    return fake


@pytest.fixture()
def no_sqlite_open(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> sqlite3.Connection:
        raise AssertionError("the host opened the database while delegating")

    monkeypatch.setattr(sqlite3, "connect", refuse)


def exec_of(*argv: str) -> list[str]:
    return ["docker", "exec", "-i", "harrier", "harrier", *argv]


# --- classes ---------------------------------------------------------------------


def leaf_parsers() -> dict[str, argparse.ArgumentParser]:
    """Every subcommand the parser accepts, by its two-word name."""

    def children(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
        for action in parser._actions:  # pyright: ignore[reportPrivateUsage]
            if isinstance(action, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage]
                choices = cast("dict[str, argparse.ArgumentParser]", action.choices)  # pyright: ignore[reportUnknownMemberType]
                return dict(choices)
        return {}

    leaves: dict[str, argparse.ArgumentParser] = {}
    for name, parser in children(build_parser()).items():
        nested = children(parser)
        if nested:
            leaves.update({f"{name} {child}": sub for child, sub in nested.items()})
        else:
            leaves[name] = parser
    return leaves


def test_every_subcommand_has_exactly_one_class() -> None:
    leaves = leaf_parsers()
    assert set(COMMAND_CLASSES) == set(leaves), (
        "unclassified: "
        f"{sorted(set(leaves) - set(COMMAND_CLASSES))}; "
        f"classified but gone: {sorted(set(COMMAND_CLASSES) - set(leaves))}"
    )
    for name, entry in COMMAND_CLASSES.items():
        assert entry.kind in (DATABASE, HOST_PATH, HOST_ONLY), name
        dests = {action.dest for action in leaves[name]._actions}  # pyright: ignore[reportPrivateUsage]
        for option in (*entry.path_options, *entry.database_options):
            # A misspelt option would silently never make the command host-path.
            assert option in dests, f"{name}: no option with destination {option!r}"


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["shortlist", "1"], DATABASE),
        (["config", "list"], DATABASE),
        (["config", "set", "feeds"], DATABASE),
        (["config", "set", "feeds", "--file", "x.json"], HOST_PATH),
        (["discover", "--scheduled"], DATABASE),
        (["discover", "--dataset-file", "x.json"], HOST_PATH),
        (["backup"], DATABASE),
        (["backup", "--dest", "/tmp/x"], HOST_PATH),
        (["export"], HOST_PATH),
        (["doctor"], HOST_ONLY),
        (["doctor", "--integrity"], DATABASE),
        (["demo-run"], HOST_ONLY),
        (["review-followup", "1"], HOST_ONLY),
    ],
)
def test_a_command_is_classed_by_its_arguments(argv: list[str], expected: str) -> None:
    args = build_parser().parse_args(argv)
    assert command_class(args) == expected, subcommand_name(args)


# --- delegation ------------------------------------------------------------------


def test_a_database_command_is_run_inside_the_container_as_the_same_vector(
    live: Path, answers: Callable[..., None], runner: FakeDocker
) -> None:
    answers(owned(live))
    runner.status = 7
    argv = ["add", "--company", "Example Co; rm -rf /", "--title", "Engineer $(id)"]
    assert main(argv) == 7
    # A vector, unjoined and unquoted: the shell-looking values reach the
    # container's argv as the strings they are.
    assert runner.calls == [exec_of(*argv)]
    assert "-t" not in runner.calls[0]


def test_a_delegated_command_never_opens_the_database_here(
    live: Path,
    answers: Callable[..., None],
    runner: FakeDocker,
    no_sqlite_open: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers(owned(live))
    # Logging not yet configured, so its setup, which opens the database for
    # the redaction values, is part of what is proved not to run.
    monkeypatch.setattr(logsetup, "_configured", False)
    assert main(["shortlist", "1"]) == 0
    gc.collect()
    assert runner.calls == [exec_of("shortlist", "1")]


def test_a_delegating_or_refusing_command_writes_no_log(
    live: Path,
    answers: Callable[..., None],
    runner: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    log = logsetup.log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("before\n", encoding="utf-8")
    monkeypatch.setattr(logsetup, "_configured", False)
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    answers(owned(live))

    main(["shortlist", "1"])
    main(["export"])

    assert log.read_text(encoding="utf-8") == "before\n"
    added = [handler for handler in root.handlers if handler not in handlers_before]
    assert not any(isinstance(handler, logging.FileHandler) for handler in added)
    assert logsetup._configured is False  # pyright: ignore[reportPrivateUsage]


def test_a_command_naming_a_host_file_is_refused_before_any_work(
    live: Path,
    answers: Callable[..., None],
    runner: FakeDocker,
    no_sqlite_open: None,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    answers(owned(live))
    dataset = tmp_path / "dataset.json"
    assert main(["discover", "--dataset-file", str(dataset)]) == 75
    err = capsys.readouterr().err
    assert "names a file on this machine" in err
    assert str(dataset) not in err
    assert runner.calls == []

    assert main(["discover", "--scheduled"]) == 0
    assert runner.calls == [exec_of("discover", "--scheduled")]


def test_a_host_path_command_runs_here_when_there_is_no_engine(
    live: Path, answers: Callable[..., None], runner: FakeDocker, tmp_path: Path
) -> None:
    answers(UNREACHABLE)
    feeds = tmp_path / "feeds.json"
    feeds.write_text('["https://boards.greenhouse.io/exampleco"]', encoding="utf-8")
    assert main(["config", "set", "feeds", "--file", str(feeds)]) == 0
    assert runner.calls == []
    conn = connect(data_dir() / "tracker.db")
    try:
        stored = conn.execute("SELECT value FROM user_config WHERE kind = 'feeds'").fetchone()
    finally:
        conn.close()
    assert stored is not None


def test_a_host_only_command_runs_here_while_the_container_runs(
    live: Path, answers: Callable[..., None], runner: FakeDocker, tmp_path: Path
) -> None:
    answers(owned(live))
    assert main(["verify-backup", str(tmp_path / "missing.tar.gz")]) == 1
    assert runner.calls == []


def test_gmail_watch_refreshes_the_token_here_before_it_is_handed_over(
    live: Path,
    answers: Callable[..., None],
    runner: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers(owned(live))
    order: list[str] = []
    monkeypatch.setattr(watch, "load_gmail_credentials", lambda: order.append("refresh"))

    def runs(command: Sequence[str]) -> int:
        order.append("exec")
        return runner(command)

    monkeypatch.setattr(delegate, "run_process", runs)
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: order.append("database"))  # type: ignore[arg-type]

    assert main(["gmail-watch"]) == 0
    assert order == ["refresh", "exec"]


def test_a_failed_token_refresh_hands_nothing_over(
    live: Path,
    answers: Callable[..., None],
    runner: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    answers(owned(live))

    def expired() -> object:
        raise RuntimeError("invalid Gmail OAuth token. Run: harrier gmail-oauth")

    monkeypatch.setattr(watch, "load_gmail_credentials", expired)
    assert main(["gmail-watch"]) == 1
    assert "token refresh failed on the host" in capsys.readouterr().err
    assert runner.calls == []


def test_a_run_whose_container_stops_is_reported_and_not_retried_here(
    live: Path,
    answers: Callable[..., None],
    runner: FakeDocker,
    no_sqlite_open: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Owned when deciding and when reading the revision; gone afterwards.
    answers(owned(live), owned(live), ContainerState(engine=container.REACHABLE))
    runner.status = 137
    assert main(["digest"]) == 137
    assert "delegated run interrupted: container stopped" in capsys.readouterr().err
    assert runner.calls == [exec_of("digest")]


def test_a_stale_image_is_named_on_the_first_line(
    live: Path,
    answers: Callable[..., None],
    runner: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    answers(owned(live, revision="abc1234"))
    monkeypatch.setattr(delegate, "working_tree_revision", lambda: "def5678-dirty")
    main(["digest"])
    first = capsys.readouterr().err.splitlines()[0]
    assert "runs revision abc1234" in first
    assert "this checkout is def5678-dirty" in first

    monkeypatch.setattr(delegate, "working_tree_revision", lambda: "abc1234")
    main(["digest"])
    assert "this checkout is" not in capsys.readouterr().err


def test_doctor_integrity_is_handed_over(
    live: Path, answers: Callable[..., None], runner: FakeDocker
) -> None:
    answers(owned(live))
    assert main(["doctor", "--integrity"]) == 0
    assert runner.calls == [exec_of("doctor", "--integrity")]


def test_without_a_docker_binary_nothing_runs_here_either(
    live: Path,
    answers: Callable[..., None],
    runner: FakeDocker,
    no_sqlite_open: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    answers(owned(live))

    def missing() -> str:
        raise delegate.DelegationError("the docker binary was not found")

    monkeypatch.setattr(delegate, "docker_binary", missing)
    assert main(["digest"]) == 1
    assert "cannot run inside the container" in capsys.readouterr().err
    assert runner.calls == []
