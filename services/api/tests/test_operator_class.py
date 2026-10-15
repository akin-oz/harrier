"""The operator class of the CLI's Postgres gate (spec 108, Amendment
(2026-10-14): the operator class is built here).

`harrier operator` is one command group, run only against a Postgres store,
each subcommand declaring the database role it runs as. The walk over every
top-level command and the texts of the other classes are spec 127's; these
tests hold the operator class: it runs on Postgres, it is refused on SQLite
before anything opens, a run is never an operator, and a subcommand cannot be
added without declaring its role.

No test reaches a server: the Postgres URL names a closed port, and the
commands' handlers are replaced by recorders.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from harrier.connection import POSTGRES_NOT_YET, RUN_OWNER_VARIABLE, URL_VARIABLE
from harrier.db import default_db_path
from harrier_api import hostedscheduler
from harrier_cli import main as cli

UNREACHABLE_URL = "postgresql://someone@127.0.0.1:1/nowhere"
SQLITE_REFUSAL = (
    "error: harrier operator {command} runs only against a hosted store; "
    "locally the schedule is launchd (harrier schedule)\n"
)
OPERATOR_COMMANDS = {
    "scheduler": ["operator", "scheduler"],
    "schedule-report": ["operator", "schedule-report"],
}
LOCAL_REFUSALS = {
    "scheduler": SQLITE_REFUSAL.format(command="scheduler"),
    "schedule-report": SQLITE_REFUSAL.format(command="schedule-report"),
    "invite": (
        "error: harrier operator invite runs only against a hosted store; "
        "HARRIER_DATABASE_URL selects one\n"
    ),
    "erase": (
        "error: harrier operator erase runs only against a hosted store; "
        "HARRIER_DATABASE_URL selects one\n"
    ),
    "erase-test-owners": (
        "error: harrier operator erase-test-owners runs only against a hosted store; "
        "HARRIER_DATABASE_URL selects one\n"
    ),
}
OWNER = "7f4f7b0e-1d2b-4c3a-9a55-0d4a1f6c2b11"


@dataclass
class Recorder:
    """Stands in for the two handlers' work: records that it ran and what it
    was given, and for the logging set-up."""

    calls: list[tuple[str, dict[str, object]]] = field(
        default_factory=lambda: list[tuple[str, dict[str, object]]]()
    )
    logging_calls: list[dict[str, object]] = field(
        default_factory=lambda: list[dict[str, object]]()
    )

    def scheduler(self, environ: Mapping[str, str], **kwargs: object) -> int:
        self.calls.append(("scheduler", {"environ": dict(environ), **kwargs}))
        return 0

    def report(self, environ: Mapping[str, str], **kwargs: object) -> int:
        self.calls.append(("schedule-report", {"environ": dict(environ), **kwargs}))
        return 1

    def logging(self, **kwargs: object) -> None:
        self.logging_calls.append(kwargs)


@pytest.fixture(name="recorder")
def recorder_fixture(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(hostedscheduler, "run_scheduler", recorder.scheduler)
    monkeypatch.setattr(hostedscheduler, "run_report", recorder.report)
    monkeypatch.setattr(cli, "configure_logging", recorder.logging)
    return recorder


def tripwire(opened: list[str], name: str) -> Callable[..., object]:
    def trip(*_args: object, **_kwargs: object) -> object:
        opened.append(name)
        raise AssertionError(f"{name} was called")

    return trip


@pytest.mark.parametrize(
    ("subcommand", "argv"),
    [
        ("scheduler", ["operator", "scheduler"]),
        ("schedule-report", ["operator", "schedule-report"]),
        ("invite", ["operator", "invite"]),
        ("erase", ["operator", "erase", OWNER, "--confirm", OWNER]),
        ("erase-test-owners", ["operator", "erase-test-owners"]),
    ],
)
def test_each_operator_command_refuses_a_local_store_for_its_own_reason(
    subcommand: str,
    argv: list[str],
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Beside SQLite the scheduler and its report say the local schedule is
    launchd (spec 108). `invite`, `erase` and `erase-test-owners` have no
    local schedule: they say the command needs a hosted store and name
    `HARRIER_DATABASE_URL`, the variable that selects one (spec 111,
    Amendment (2026-10-15)). Nothing is run."""
    monkeypatch.chdir(tmp_path)
    assert cli.main(argv) == 1
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", LOCAL_REFUSALS[subcommand])
    assert recorder.calls == []
    if subcommand not in ("scheduler", "schedule-report"):
        assert "launchd" not in captured.err
        assert "HARRIER_DATABASE_URL" in captured.err


def test_the_operator_class_runs_only_on_postgres(
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """On a Postgres URL both subcommands run, under hosted logging. On a
    SQLite store each is refused, exit 1, before anything opens: no lease, no
    hand-over, no store. With `HARRIER_RUN_OWNER` set a run is refused, as
    every command outside the run class is. No other command is an operator
    command."""
    opened: list[str] = []
    before = sorted(str(path) for path in default_db_path().parent.rglob("*"))
    for name in ("connect", "running_in", "remove_dead", "set_lease_subcommand", "open_store"):
        monkeypatch.setattr(cli, name, tripwire(opened, name))
    monkeypatch.chdir(tmp_path)

    # SQLite: the text, naming the command words only, and nothing opened.
    for subcommand, argv in OPERATOR_COMMANDS.items():
        assert cli.main(argv) == 1
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == ("", SQLITE_REFUSAL.format(command=subcommand))
    assert opened == [] and recorder.calls == []
    assert sorted(str(path) for path in default_db_path().parent.rglob("*")) == before

    # Postgres: both run, each reaching its handler with the process
    # environment, and the report's exit status is the command's.
    monkeypatch.setenv(URL_VARIABLE, UNREACHABLE_URL)
    assert cli.main(OPERATOR_COMMANDS["scheduler"]) == 0
    assert cli.main(OPERATOR_COMMANDS["schedule-report"]) == 1
    assert [name for name, _ in recorder.calls] == ["scheduler", "schedule-report"]
    scheduler_call = recorder.calls[0][1]
    assert scheduler_call["environ"] == dict(os.environ)
    # The scheduler asks for its signals: Fly stops a tick with SIGINT, then SIGTERM.
    assert scheduler_call["handle_signals"] is True
    # Hosted logging: stderr only, no local tracker.
    assert recorder.logging_calls == [{"hosted": True}, {"hosted": True}]
    assert opened == []

    # A run is never an operator: with an owner set, the gate gives the one
    # text every command outside the run class gets.
    recorder.calls.clear()
    monkeypatch.setenv(RUN_OWNER_VARIABLE, OWNER)
    for argv in OPERATOR_COMMANDS.values():
        assert cli.main(argv) == 1
        assert capsys.readouterr().err == f"error: {POSTGRES_NOT_YET}\n"
    assert recorder.calls == []
    monkeypatch.delenv(RUN_OWNER_VARIABLE)

    # No other command is an operator command: the others keep the gate they
    # had on a Postgres URL.
    for argv in (["next"], ["tracks", "list"]):
        assert cli.main(argv) == 1
        assert capsys.readouterr().err == f"error: {POSTGRES_NOT_YET}\n"
    assert cli.main(["discover"]) == 1
    assert capsys.readouterr().err == f"error: {cli.run_class_refusal('discover')}\n"
    assert recorder.calls == []


# --- the declarations ---


def test_every_operator_subcommand_declares_its_role() -> None:
    """Each `harrier operator` subcommand declares a database role or that it
    opens none, a subcommand added without one is found by the walk, and
    `scheduler` and `schedule-report` each declare `harrier_scheduler`, `erase`
    and `erase-test-owners` declare `service_role` and `invite` declares that it
    opens no database connection (specs 111 and 143)."""
    parser = cli.build_parser()
    assert cli.operator_roles(parser) == {
        "scheduler": "harrier_scheduler",
        "schedule-report": "harrier_scheduler",
        "invite": cli.NO_DATABASE,
        "erase": "service_role",
        "erase-test-owners": "service_role",
    }
    assert cli.undeclared_operator_subcommands(parser) == []

    # A subcommand added without a declaration is named.
    cli.operator_group(parser).add_parser("another")
    assert cli.operator_roles(parser)["another"] is None
    assert cli.undeclared_operator_subcommands(parser) == ["another"]

    # One that declares it opens no database connection is declared.
    declared = cli.build_parser()
    cli._operator_subcommand(  # pyright: ignore[reportPrivateUsage]
        cli.operator_group(declared),
        "another",
        role=cli.NO_DATABASE,
        func=lambda _args: 0,
        help="another command",
    )
    assert cli.undeclared_operator_subcommands(declared) == []
    assert cli.operator_roles(declared)["another"] == cli.NO_DATABASE


def test_no_other_command_is_an_operator_command() -> None:
    """The group is the only place a role is declared: no other top-level
    command, and no subcommand of one, carries an operator role."""
    top = cli._subparsers_of(cli.build_parser())  # pyright: ignore[reportPrivateUsage]
    assert "operator" in top
    for name, command in top.items():
        if name == "operator":
            continue
        assert command.get_default(cli.OPERATOR_ROLE) is None, name
        for nested_name, nested in cli._subparsers_of(command).items():  # pyright: ignore[reportPrivateUsage]
            assert nested.get_default(cli.OPERATOR_ROLE) is None, (name, nested_name)
    assert top["operator"].get_default(cli.OPERATOR_ROLE) is None
