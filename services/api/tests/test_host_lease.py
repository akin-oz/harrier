"""A host lease keeps the container out while a host run holds the database (spec 075).

The tests play both sides of the bind mount in one process: `running_in` is
stubbed to "host" or "container", `live_data_root` points at a temporary
directory standing in for the mounted `data/`, and the engine is a fake. No
test reaches Docker, and the fake `docker exec` is autouse, because the real
container may be running on the machine executing the suite.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from harrier import container, db, delegate, hostlease
from harrier.container import ContainerState
from harrier.db import DatabaseOwnedByContainer, DatabaseOwnedByHost, connect
from harrier_api.app import create_app
from harrier_api.deps import get_conn
from harrier_api.runs import RunManager
from harrier_cli import main as cli
from harrier_cli.main import main

UNREACHABLE = ContainerState(engine=container.UNREACHABLE)
SENTINEL = "Sentinel Person 7f3a"


@pytest.fixture()
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "live-data"
    root.mkdir()
    monkeypatch.setattr(db, "live_data_root", lambda: root)
    monkeypatch.setenv("HARRIER_DATA_DIR", str(root))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    monkeypatch.setattr(container, "detect", lambda timeout=0.0: UNREACHABLE)
    monkeypatch.setattr(container, "running_in", lambda: "host")
    yield root
    db.release_host_lease()


def owned(root: Path) -> ContainerState:
    return ContainerState(engine=container.REACHABLE, running=True, data_source=root)


def leases(root: Path) -> list[Path]:
    return hostlease.lease_files(root / hostlease.LEASE_DIRNAME)


def make_database(path: Path) -> None:
    conn = connect(path)
    conn.close()
    db.release_host_lease()


class FakeDocker:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.on_call: Callable[[], None] | None = None

    def __call__(self, command: Sequence[str]) -> int:
        if self.on_call is not None:
            self.on_call()
        self.calls.append(list(command))
        return 0


@pytest.fixture(autouse=True)
def runner(monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    fake = FakeDocker()
    monkeypatch.setattr(delegate, "run_process", fake)
    monkeypatch.setattr(delegate, "docker_binary", lambda: "docker")
    monkeypatch.setattr(delegate, "working_tree_revision", lambda: None)
    return fake


def a_lease_from(root: Path, subcommand: str) -> Path:
    """A lease as a live host process would write it: this test process's."""
    return hostlease.write_lease(root / hostlease.LEASE_DIRNAME, os.getpid(), subcommand)


def as_container(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(container, "running_in", lambda: "container")


# --- the host takes its lease, then asks --------------------------------------------


def test_the_lease_comes_before_the_question_and_goes_with_the_command(
    live: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_database(live / "tracker.db")
    seen: list[int] = []

    def detect(timeout: float = 0.0) -> ContainerState:
        seen.append(len(leases(live)))
        return UNREACHABLE

    monkeypatch.setattr(container, "detect", detect)
    assert main(["next"]) == 0
    # The CLI's probe asks with no lease; its second question is asked
    # holding one. Logging and the command's own opens ask nothing more.
    assert seen[:2] == [0, 1]
    assert leases(live) == []


def test_the_lease_goes_when_the_command_raises(
    live: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_database(live / "tracker.db")
    held: list[int] = []

    def explode(args: object) -> int:
        held.append(len(leases(live)))
        raise ValueError("an unexpected failure inside the command")

    # The parser reads the command function when it is built, inside `main`.
    monkeypatch.setattr(cli, "_cmd_tracker_verb", explode)
    with pytest.raises(ValueError, match="unexpected failure"):
        main(["next"])
    assert held == [1]
    assert leases(live) == []


def test_a_refused_open_leaves_no_lease(live: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_database(live / "tracker.db")
    monkeypatch.setattr(container, "detect", lambda timeout=0.0: owned(live))
    with pytest.raises(DatabaseOwnedByContainer):
        connect()
    assert leases(live) == []


def test_a_container_that_starts_after_the_first_question_gets_the_command(
    live: Path, monkeypatch: pytest.MonkeyPatch, runner: FakeDocker
) -> None:
    """Down at the probe, up by the time the lease is taken: handed over, and
    the lease is gone."""
    make_database(live / "tracker.db")
    answers = iter([UNREACHABLE])
    monkeypatch.setattr(container, "detect", lambda timeout=0.0: next(answers, owned(live)))
    assert main(["shortlist", "1"]) == 0
    assert runner.calls == [["docker", "exec", "-i", "harrier", "harrier", "shortlist", "1"]]
    assert leases(live) == []


def test_a_handed_over_command_never_takes_a_lease(
    live: Path, monkeypatch: pytest.MonkeyPatch, runner: FakeDocker
) -> None:
    make_database(live / "tracker.db")
    monkeypatch.setattr(container, "detect", lambda timeout=0.0: owned(live))
    seen_during: list[list[Path]] = []
    runner.on_call = lambda: seen_during.append(leases(live))
    assert main(["digest"]) == 0
    assert seen_during == [[]]
    assert leases(live) == []


def test_a_lease_holds_the_pid_the_start_and_the_subcommand_only(
    live: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    make_database(live / "tracker.db")
    captured: list[str] = []
    real_write = hostlease.write_lease

    def write_and_look(directory: Path, pid: int, subcommand: str) -> Path:
        path = real_write(directory, pid, subcommand)
        captured.append(path.read_text(encoding="utf-8"))
        return path

    monkeypatch.setattr(hostlease, "write_lease", write_and_look)
    assert main(["add", "--company", SENTINEL, "--title", SENTINEL]) == 0
    assert len(captured) == 1
    lease = json.loads(captured[0])
    assert set(lease) == {"pid", "started", "subcommand"}
    assert lease["pid"] == os.getpid()
    assert lease["subcommand"] == "add"
    assert SENTINEL not in captured[0]

    # The same lease, as the container and doctor see it.
    (live / hostlease.LEASE_DIRNAME / f"{os.getpid()}.json").write_text(
        captured[0], encoding="utf-8"
    )
    as_container(monkeypatch)
    client = TestClient(create_app())
    assert SENTINEL not in client.get("/api/jobs").text
    assert SENTINEL not in client.get("/health").text
    capsys.readouterr()
    main(["doctor"])
    assert SENTINEL not in capsys.readouterr().out


# --- the container side and the API ----------------------------------------------


def test_inside_the_container_any_lease_refuses_the_open_and_the_api_answers_503(
    live: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_database(live / "tracker.db")
    a_lease_from(live, "discover")
    as_container(monkeypatch)
    with pytest.raises(DatabaseOwnedByHost) as raised:
        connect()
    assert raised.value.subcommand == "discover"

    response = TestClient(create_app()).get("/jobs")
    assert response.status_code == 503
    body = response.json()
    assert body["hold"]["subcommand"] == "discover"
    assert body["hold"]["since"] == hostlease.process_start_time(os.getpid())
    assert body["detail"]


def test_an_unreadable_lease_still_holds_the_container_out(
    live: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_database(live / "tracker.db")
    directory = live / hostlease.LEASE_DIRNAME
    directory.mkdir(exist_ok=True)
    (directory / "123.json").write_text("{not json", encoding="utf-8")
    as_container(monkeypatch)
    with pytest.raises(DatabaseOwnedByHost):
        connect()


def test_health_reports_the_hold_without_opening_the_database(
    live: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_database(live / "tracker.db")
    as_container(monkeypatch)
    client = TestClient(create_app())

    body = client.get("/health").json()
    assert (body["database_hold"], isinstance(body["job_count"], int)) == (None, True)

    # Another process's lease: this process's own is never a hold against it.
    directory = live / hostlease.LEASE_DIRNAME
    directory.mkdir(exist_ok=True)
    (directory / "424242.json").write_text(
        json.dumps({"pid": 424242, "started": "2026-10-05T07:00:00+00:00", "subcommand": "digest"}),
        encoding="utf-8",
    )
    opened: list[object] = []

    def refuse(*args: object, **kwargs: object) -> sqlite3.Connection:
        opened.append(args)
        raise AssertionError("/health opened the database while a host lease exists")

    monkeypatch.setattr(sqlite3, "connect", refuse)
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["job_count"] is None
    assert body["database_hold"] == {"subcommand": "digest", "since": "2026-10-05T07:00:00+00:00"}
    assert opened == []


def test_health_does_not_count_its_own_lease_as_a_hold(live: Path) -> None:
    """A host uvicorn takes a lease for itself when it opens the database;
    its own /health must not then report itself as holding it out."""
    make_database(live / "tracker.db")
    a_lease_from(live, "python")
    body = TestClient(create_app()).get("/health").json()
    assert body["database_hold"] is None
    assert isinstance(body["job_count"], int)


def api_routes(routes: Sequence[object]) -> Iterator[APIRoute]:
    """Every APIRoute, including those inside included routers: FastAPI 0.141
    keeps an included router whole rather than copying its routes."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from api_routes(original.routes)


def test_every_route_that_opens_the_database_declares_the_503() -> None:
    from fastapi.dependencies.models import Dependant

    def needs_conn(dependant: Dependant) -> bool:
        return dependant.call is get_conn or any(needs_conn(d) for d in dependant.dependencies)

    app = create_app()
    routes = list(api_routes(app.routes))
    with_conn = {
        (method.lower(), route.path)
        for route in routes
        if needs_conn(route.dependant)
        for method in route.methods or ()
    }
    assert len(with_conn) > 10, "the walk is not finding the routes that open the database"
    # Judged on the contract, which is what a client sees: exactly the routes
    # that open the database declare the 503, and no other.
    spec = app.openapi()
    declared = {
        (method, path)
        for path, operations in spec["paths"].items()
        for method, operation in operations.items()
        if "503" in operation.get("responses", {})
    }
    assert declared == with_conn


def test_a_web_run_refused_by_a_lease_ends_75_with_the_refusal_logged(
    live: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real subprocess running the real CLI as the container would, under
    the real run manager. The child cannot inherit monkeypatches, so it sets
    the same three things itself before calling `main`."""
    make_database(live / "tracker.db")
    a_lease_from(live, "digest")
    script = textwrap.dedent(
        f"""
        import sys
        from pathlib import Path
        from harrier import container, db
        from harrier.container import ContainerState
        db.live_data_root = lambda: Path({str(live)!r})
        container.running_in = lambda: "container"
        container.detect = lambda timeout=0.0: ContainerState(engine=container.UNREACHABLE)
        from harrier_cli.main import main
        sys.exit(main(["next"]))
        """
    )

    async def scenario() -> tuple[int | None, list[str]]:
        manager = RunManager(
            journal_path=tmp_path / "journal.jsonl",
            kind_commands={"demo": [sys.executable, "-c", script]},
            grace_seconds=0.5,
        )
        run = await manager.start("demo")
        finished = await manager.wait(run.id)
        lines = [str(event.data.get("line", "")) for event in finished.events]
        return finished.exit_code, lines

    code, lines = asyncio.run(scenario())
    assert code == 75
    assert any("a host process (digest" in line for line in lines), lines


# --- liveness ---------------------------------------------------------------------


def test_a_host_invocation_clears_dead_leases_and_keeps_live_ones(
    live: Path, tmp_path: Path
) -> None:
    directory = live / hostlease.LEASE_DIRNAME
    directory.mkdir(exist_ok=True)
    # A process that is gone.
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    (directory / f"{gone.pid}.json").write_text(
        json.dumps({"pid": gone.pid, "started": "2026-01-01T00:00:00+00:00", "subcommand": "x"}),
        encoding="utf-8",
    )
    # A live pid whose start time does not match: the pid was reused.
    (directory / "1.json").write_text(
        json.dumps({"pid": 1, "started": "1999-01-01T00:00:00+00:00", "subcommand": "x"}),
        encoding="utf-8",
    )
    # A live process with the matching start time: still holding.
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        started = hostlease.process_start_time(sleeper.pid)
        (directory / f"{sleeper.pid}.json").write_text(
            json.dumps({"pid": sleeper.pid, "started": started, "subcommand": "x"}),
            encoding="utf-8",
        )
        main(["doctor"])
        remaining = sorted(path.name for path in hostlease.lease_files(directory))
    finally:
        sleeper.kill()
        sleeper.wait()
    assert remaining == [f"{sleeper.pid}.json"]


def test_a_stale_lease_carrying_this_pid_is_replaced(live: Path) -> None:
    directory = live / hostlease.LEASE_DIRNAME
    directory.mkdir(exist_ok=True)
    stale = directory / f"{os.getpid()}.json"
    stale.write_text(
        json.dumps(
            {"pid": os.getpid(), "started": "1999-01-01T00:00:00+00:00", "subcommand": "old"}
        ),
        encoding="utf-8",
    )
    # One open, with the stale file in place: no earlier open whose release
    # would delete it first and let a fresh write pass for a replacement.
    db.set_lease_subcommand("next")
    try:
        connect().close()
        lease = hostlease.read_lease(stale)
    finally:
        db.set_lease_subcommand("python")
    assert lease is not None
    assert lease.subcommand == "next"
    assert lease.started == hostlease.process_start_time(os.getpid())


# --- harrier doctor ---------------------------------------------------------------


def test_doctor_lists_leases_and_flags_a_dead_one_and_a_contested_one(
    live: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = live / hostlease.LEASE_DIRNAME
    directory.mkdir(exist_ok=True)
    # Doctor runs `remove_dead` first through `main`, so call it directly to
    # see the dead lease reported rather than silently cleared.
    from harrier.doctor import run_doctor

    (directory / "999999.json").write_text(
        json.dumps({"pid": 999999, "started": "2026-01-01T00:00:00+00:00", "subcommand": "digest"}),
        encoding="utf-8",
    )
    result = run_doctor()
    assert "host leases: 1" in result.lines
    assert any(
        line.endswith("digest, since 2026-01-01T00:00:00+00:00, dead") for line in result.lines
    )
    assert result.exit_code == 1

    (directory / "999999.json").unlink()
    a_lease_from(live, "next")
    assert run_doctor().exit_code == 0
    monkeypatch.setattr(container, "detect", lambda timeout=0.0: owned(live))
    contested = run_doctor()
    assert any(line.endswith(", live") for line in contested.lines)
    assert contested.exit_code == 1
