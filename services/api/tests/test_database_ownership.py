"""The host refuses the tracker database while the container owns it (spec 061).

Every test here decides who owns the database with a fake: either a stubbed
`harrier.container.detect`, or a real unix-socket server playing the Docker
engine, so the socket and HTTP code is exercised rather than replaced. None
of them needs Docker, and none of them reaches the operator's data directory:
`live_data_root` is pointed at a temporary directory that stands in for it.
"""

from __future__ import annotations

import gc
import hashlib
import http.server
import json
import logging
import socket
import socketserver
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from harrier import backup, container, db, delegate, logsetup
from harrier.container import ContainerState
from harrier.db import DatabaseOwnedByContainer, DatabaseOwnershipUnknown, connect
from harrier_cli.main import main

UNREACHABLE = ContainerState(engine=container.UNREACHABLE)
NOT_RUNNING = ContainerState(engine=container.REACHABLE)
ENGINE_ERROR = ContainerState(engine=container.UNKNOWN, reason="the docker engine did not answer")


@pytest.fixture(autouse=True)
def no_real_docker(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """No test here may run the real `docker exec`: these tests fake a running
    container, and the real one may be running too. Delegated commands are
    recorded instead, and answer 0 (spec 074)."""
    calls: list[list[str]] = []

    def record(command: object) -> int:
        calls.append(list(command))  # type: ignore[call-overload]
        return 0

    monkeypatch.setattr(delegate, "run_process", record)
    monkeypatch.setattr(delegate, "docker_binary", lambda: "docker")
    return calls


@pytest.fixture()
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data directory that plays the part of the one the container mounts."""
    root = tmp_path / "live-data"
    root.mkdir()
    monkeypatch.setattr(db, "live_data_root", lambda: root)
    monkeypatch.setenv("HARRIER_DATA_DIR", str(root))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    return root


def owned_by_container(root: Path) -> ContainerState:
    return ContainerState(engine=container.REACHABLE, running=True, data_source=root)


@pytest.fixture()
def detector(monkeypatch: pytest.MonkeyPatch) -> Callable[[ContainerState], list[int]]:
    """Set what the engine says. Returns a list that counts the questions."""

    def install(state: ContainerState) -> list[int]:
        asked: list[int] = []

        def detect(timeout: float = container.DEFAULT_TIMEOUT_SECONDS) -> ContainerState:
            asked.append(1)
            return state

        monkeypatch.setattr(container, "detect", detect)
        return asked

    return install


@pytest.fixture()
def no_sqlite_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything reaches sqlite3.connect."""

    def refuse(*args: object, **kwargs: object) -> sqlite3.Connection:
        raise AssertionError("sqlite3.connect was called on a database the host may not open")

    monkeypatch.setattr(sqlite3, "connect", refuse)


def make_database(path: Path, rows: int = 3) -> None:
    """A synthetic harrier-shaped database, built before any detector is set."""
    conn = connect(path)
    try:
        for index in range(rows):
            conn.execute(
                "INSERT INTO jobs (company, title, url, status) VALUES (?, ?, ?, 'prospect')",
                (f"Example Co {index}", "Engineer", f"https://example.test/{index}"),
            )
        conn.commit()
    finally:
        conn.close()


def tree_digest(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# --- the guard -----------------------------------------------------------------


def test_connect_is_refused_before_sqlite_opens_anything(
    live: Path, detector: Callable[[ContainerState], list[int]], no_sqlite_open: None
) -> None:
    detector(owned_by_container(live))
    with pytest.raises(DatabaseOwnedByContainer, match="docker exec harrier harrier"):
        connect()


def test_the_backup_snapshot_and_verify_are_refused_on_the_live_file(
    live: Path,
    detector: Callable[[ContainerState], list[int]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_database(live / "tracker.db")
    detector(owned_by_container(live))

    def refuse(*args: object, **kwargs: object) -> sqlite3.Connection:
        raise AssertionError("sqlite3.connect was called on the live database")

    monkeypatch.setattr(sqlite3, "connect", refuse)
    with pytest.raises(DatabaseOwnedByContainer):
        backup.snapshot_database(live / "tracker.db", tmp_path / "copy.db")
    with pytest.raises(DatabaseOwnedByContainer):
        backup.verify_database(live / "tracker.db")
    assert not (tmp_path / "copy.db").exists()


def test_create_and_restore_are_refused_and_touch_nothing(
    live: Path, detector: Callable[[ContainerState], list[int]], tmp_path: Path
) -> None:
    make_database(live / "tracker.db")
    (live / "notes.txt").write_text("synthetic\n", encoding="utf-8")
    archives = tmp_path / "archives"
    detector(UNREACHABLE)
    taken = backup.create_backup(archives)
    before = tree_digest(live)

    detector(owned_by_container(live))
    with pytest.raises(DatabaseOwnedByContainer):
        backup.create_backup(archives)
    with pytest.raises(DatabaseOwnedByContainer):
        backup.restore_backup(taken.archive, force=True)

    assert tree_digest(live) == before
    assert sorted(path.name for path in archives.iterdir()) == [taken.archive.name]


def test_an_engine_that_cannot_answer_refuses_and_no_engine_allows(
    live: Path, detector: Callable[[ContainerState], list[int]]
) -> None:
    detector(ENGINE_ERROR)
    with pytest.raises(DatabaseOwnershipUnknown, match="did not answer"):
        connect()
    detector(UNREACHABLE)
    connect().close()
    detector(NOT_RUNNING)
    connect().close()


def test_a_database_outside_the_mounted_directory_is_never_refused(
    live: Path, detector: Callable[[ContainerState], list[int]], tmp_path: Path
) -> None:
    asked = detector(owned_by_container(live))
    # A test's own database, a demo directory, an extracted backup: not under
    # the mounted directory, so the engine is not even asked.
    connect(tmp_path / "elsewhere" / "tracker.db").close()
    assert asked == []

    # The live directory, while the container has some other directory: allowed.
    detector(
        ContainerState(engine=container.REACHABLE, running=True, data_source=tmp_path / "other")
    )
    connect().close()


def test_logging_setup_under_refusal_writes_no_log_file_and_the_open_is_still_refused(
    live: Path, detector: Callable[[ContainerState], list[int]]
) -> None:
    """Amended spec 061: logging setup must not refuse every command, and must
    not swallow the refusal either. It writes no harrier.log, and the
    command's own open is still refused."""
    detector(owned_by_container(live))
    try:
        logsetup.configure_logging(force=True)
        handlers = logging.getLogger().handlers
        assert not any(isinstance(handler, logging.FileHandler) for handler in handlers)
        assert not logsetup.log_path().exists()
        with pytest.raises(DatabaseOwnedByContainer):
            connect()
    finally:
        detector(UNREACHABLE)
        logsetup.configure_logging(force=True)


# --- the CLI ---------------------------------------------------------------------


def test_a_refused_command_exits_75_and_names_the_way_out_without_its_arguments(
    live: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    no_real_docker: list[list[str]],
) -> None:
    """The guard inside a command: the container was down when the CLI
    decided to run here, and up by the time the command opened the file.
    Since spec 074 a command decided while the container is up is delegated,
    so this race is where spec 061's refusal is still reached from the CLI."""
    make_database(live / "tracker.db")
    answers = iter([UNREACHABLE])

    def detect(timeout: float = container.DEFAULT_TIMEOUT_SECONDS) -> ContainerState:
        return next(answers, owned_by_container(live))

    monkeypatch.setattr(container, "detect", detect)
    sentinel = "Sentinel Person 7f3a"

    assert main(["shortlist", sentinel]) == 75

    err = capsys.readouterr().err
    assert "harrier container owns the tracker database" in err
    assert "docker exec harrier harrier shortlist <arguments>" in err
    assert "harrier doctor" in err
    assert sentinel not in err
    assert no_real_docker == []


def test_a_nested_subcommand_is_named_in_full(
    live: Path,
    detector: Callable[[ContainerState], list[int]],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    detector(owned_by_container(live))
    assert main(["config", "set", "feeds", "--file", str(tmp_path / "feeds.json")]) == 75
    err = capsys.readouterr().err
    assert err.startswith("harrier config set: refused")
    assert str(tmp_path) not in err


def test_an_unknown_owner_refuses_with_the_reason(
    live: Path,
    detector: Callable[[ContainerState], list[int]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    detector(ENGINE_ERROR)
    assert main(["config", "list"]) == 75
    assert "did not answer" in capsys.readouterr().err


# --- harrier doctor --------------------------------------------------------------


def doctor(capsys: pytest.CaptureFixture[str], *flags: str) -> tuple[int, list[str]]:
    capsys.readouterr()
    code = main(["doctor", *flags])
    return code, capsys.readouterr().out.splitlines()


def test_doctor_reports_each_line_and_exits_0_whoever_owns_the_file(
    live: Path,
    detector: Callable[[ContainerState], list[int]],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    make_database(live / "tracker.db")

    detector(owned_by_container(live))
    code, lines = doctor(capsys)
    assert code == 0
    assert lines == [
        "running in: host",
        "docker engine: reachable",
        "harrier container: running",
        "data mount: this repository",
        "journal mode: wal",
        "wal file: absent",
        "shm file: absent",
        "host access: refused (container owns the database)",
    ]

    detector(UNREACHABLE)
    code, lines = doctor(capsys)
    assert code == 0
    assert lines[1:4] == [
        "docker engine: not reachable",
        "harrier container: not running",
        "data mount: none",
    ]
    assert lines[-1] == "host access: allowed"

    detector(ContainerState(engine=container.REACHABLE, running=True, data_source=tmp_path / "x"))
    code, lines = doctor(capsys)
    assert (code, lines[3], lines[-1]) == (0, "data mount: elsewhere", "host access: allowed")

    for line in lines:
        assert str(tmp_path) not in line
        assert str(Path.home()) not in line


def test_doctor_exits_1_when_ownership_is_unknown(
    live: Path,
    detector: Callable[[ContainerState], list[int]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    detector(ENGINE_ERROR)
    code, lines = doctor(capsys)
    assert code == 1
    assert lines[-1].startswith("host access: refused (ownership unknown:")


def test_require_host_access_exits_75_unless_access_is_allowed(
    live: Path,
    detector: Callable[[ContainerState], list[int]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    detector(owned_by_container(live))
    assert doctor(capsys, "--require-host-access")[0] == 75
    detector(UNREACHABLE)
    assert doctor(capsys, "--require-host-access")[0] == 0


def damage(path: Path) -> None:
    """Overwrite the middle of a page past the schema, as a torn write would."""
    size = path.stat().st_size
    page = 4096
    assert size >= page * 4, "the fixture database is too small to damage a data page"
    with path.open("r+b") as handle:
        handle.seek(page * 2 + 8)
        handle.write(b"\xff" * 512)


def test_integrity_exits_1_on_a_damaged_page_and_0_on_a_good_database(
    live: Path,
    detector: Callable[[ContainerState], list[int]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_database(live / "tracker.db", rows=400)
    detector(UNREACHABLE)

    code, lines = doctor(capsys, "--integrity")
    assert (code, lines[-1]) == (0, "integrity: ok")

    damage(live / "tracker.db")
    code, lines = doctor(capsys, "--integrity")
    assert code == 1
    assert lines[-1] != "integrity: ok"


def test_integrity_does_not_open_the_file_the_container_owns(
    live: Path,
    detector: Callable[[ContainerState], list[int]],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    no_real_docker: list[list[str]],
) -> None:
    """Since spec 074 the check runs inside the container instead; the host
    still never opens the file."""
    make_database(live / "tracker.db")
    detector(owned_by_container(live))
    opened: list[object] = []
    real_connect = sqlite3.connect

    def watch(*args: object, **kwargs: object) -> sqlite3.Connection:
        opened.append(args[0] if args else None)
        return real_connect(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sqlite3, "connect", watch)
    assert main(["doctor", "--integrity"]) == 0
    assert no_real_docker == [
        ["docker", "exec", "-i", "harrier", "harrier", "doctor", "--integrity"]
    ]
    assert opened == []


def leave_a_crashed_writers_wal(path: Path) -> None:
    """Commit a row in a child process that exits without closing, so its
    WAL is never checkpointed: what a killed writer leaves behind."""
    code = (
        "import os, sqlite3, sys\n"
        "conn = sqlite3.connect(sys.argv[1])\n"
        "conn.execute('PRAGMA wal_autocheckpoint=0')\n"
        'conn.execute("INSERT INTO jobs (company, title, url, status) '
        "VALUES ('Late Co', 'Engineer', 'https://example.test/late', 'prospect')\")\n"
        "conn.commit()\n"
        "os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-c", code, str(path)], check=True)
    assert path.with_name(path.name + "-wal").stat().st_size > 0


def snapshot_files(root: Path) -> dict[str, str]:
    """The database and its WAL, by content; the -shm index by presence only.

    The -shm file is SQLite's shared-memory index of the WAL, not data: every
    reader, read-only ones included, rebuilds it from the WAL. The evidence a
    crash leaves is the database and the WAL, and those must not change.
    """
    files: dict[str, str] = {}
    for path in sorted(root.iterdir()):
        if path.name.endswith("-shm"):
            files[path.name] = "present"
        elif path.name.startswith("tracker.db"):
            files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def test_integrity_leaves_a_crashed_writers_wal_and_the_database_untouched(
    live: Path,
    detector: Callable[[ContainerState], list[int]],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read-write open closed last checkpoints the WAL into the database and
    deletes it: the check would change the file it reports on and destroy the
    evidence of the crash (review finding on PR #110)."""
    make_database(live / "tracker.db")
    leave_a_crashed_writers_wal(live / "tracker.db")
    before = snapshot_files(live)
    detector(UNREACHABLE)
    # Through `main`, with logging not yet configured, so the proof covers
    # logging setup too: it opened the database read-write before `doctor`
    # ran and checkpointed it itself. `configure_logging` is idempotent, so
    # without the reset an earlier test would have made that call a no-op.
    monkeypatch.setattr(logsetup, "_configured", False)
    try:
        code, lines = doctor(capsys, "--integrity")
    finally:
        # A connection left open is closed, and checkpointed, when it is
        # collected: at process exit for a real CLI run. Collect now so the
        # comparison sees what a finished `harrier doctor` would leave.
        gc.collect()
    assert (code, lines[-1]) == (0, "integrity: ok")
    assert snapshot_files(live) == before
    logsetup.configure_logging(force=True)


def test_integrity_on_a_clean_database_creates_no_files(
    live: Path,
    detector: Callable[[ContainerState], list[int]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_database(live / "tracker.db")
    before = snapshot_files(live)
    assert set(before) == {"tracker.db"}
    detector(UNREACHABLE)
    code, lines = doctor(capsys, "--integrity")
    assert (code, lines[-1]) == (0, "integrity: ok")
    assert snapshot_files(live) == before


def test_doctor_still_gives_a_verdict_when_the_database_cannot_be_read(
    live: Path,
    detector: Callable[[ContainerState], list[int]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_database(live / "tracker.db")
    detector(owned_by_container(live))
    (live / "tracker.db").chmod(0)
    try:
        code, lines = doctor(capsys)
    finally:
        (live / "tracker.db").chmod(0o644)
    assert code == 0
    assert "journal mode: unreadable" in lines
    assert lines[-1] == "host access: refused (container owns the database)"


def test_logging_setup_does_not_blame_the_container_when_ownership_is_unknown(
    live: Path,
    detector: Callable[[ContainerState], list[int]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    detector(ENGINE_ERROR)
    try:
        logsetup.configure_logging(force=True)
        err = capsys.readouterr().err
        assert "container owns" not in err
        assert "cannot tell who owns the database" in err
    finally:
        detector(UNREACHABLE)
        logsetup.configure_logging(force=True)


# --- the detector, against a fake engine on a real socket -------------------------


@pytest.fixture()
def engine(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[int, object], None]]:
    """A unix-socket HTTP server answering the engine's container lookup.

    The socket lives in a short temporary directory: macOS limits a unix
    socket path to 104 bytes, which a pytest tmp_path exceeds.
    """
    directory = Path(tempfile.mkdtemp(prefix="hd", dir="/tmp"))
    socket_path = directory / "engine.sock"
    reply: dict[str, object] = {"status": 404, "body": {}}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = json.dumps(reply["body"]).encode()
            self.send_response(int(str(reply["status"])))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    server = Server(str(socket_path), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("DOCKER_HOST", f"unix://{socket_path}")

    def answer(status: int, body: object) -> None:
        reply["status"] = status
        reply["body"] = body

    try:
        yield answer
    finally:
        server.shutdown()
        server.server_close()
        socket_path.unlink(missing_ok=True)
        directory.rmdir()


def test_the_detector_reads_the_engine_answer(engine: Callable[[int, object], None]) -> None:
    engine(404, {"message": "No such container"})
    assert container.detect() == NOT_RUNNING

    engine(200, {"State": {"Running": False}, "Mounts": []})
    assert container.detect() == NOT_RUNNING

    engine(
        200,
        {
            "State": {"Running": True},
            "Mounts": [
                {"Destination": "/app/config", "Source": "/host/repo/config"},
                {"Destination": "/app/data", "Source": "/host/repo/data"},
            ],
        },
    )
    assert container.detect() == ContainerState(
        engine=container.REACHABLE, running=True, data_source=Path("/host/repo/data")
    )

    engine(500, {"message": "boom"})
    state = container.detect()
    assert (state.engine, state.reason) == (
        container.UNKNOWN,
        "the docker engine answered HTTP 500",
    )


def test_the_detector_reads_the_image_revision(engine: Callable[[int, object], None]) -> None:
    """Spec 074 names a stale image on a delegated run's first line."""
    engine(
        200,
        {
            "State": {"Running": True},
            "Config": {"Env": ["PATH=/usr/bin", "HARRIER_REVISION=abc1234-dirty"]},
            "Mounts": [{"Destination": "/app/data", "Source": "/host/repo/data"}],
        },
    )
    assert container.detect().revision == "abc1234-dirty"

    engine(200, {"State": {"Running": True}, "Config": {"Env": []}, "Mounts": []})
    assert container.detect().revision is None


def test_a_socket_with_nothing_listening_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    directory = Path(tempfile.mkdtemp(prefix="hd", dir="/tmp"))
    try:
        socket_path = directory / "dead.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path))
        listener.close()  # the file stays; nobody accepts on it
        monkeypatch.setenv("DOCKER_HOST", f"unix://{socket_path}")
        assert container.detect() == UNREACHABLE

        monkeypatch.setenv("DOCKER_HOST", f"unix://{directory / 'missing.sock'}")
        assert container.detect() == UNREACHABLE
    finally:
        for path in directory.iterdir():
            path.unlink()
        directory.rmdir()


def test_a_socket_this_user_may_not_use_is_named_as_such(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = Path(tempfile.mkdtemp(prefix="hd", dir="/tmp"))
    socket_path = directory / "locked.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    socket_path.chmod(0)
    monkeypatch.setenv("DOCKER_HOST", f"unix://{socket_path}")
    try:
        state = container.detect(timeout=0.3)
    finally:
        listener.close()
        socket_path.unlink(missing_ok=True)
        directory.rmdir()
    # Still refused: an engine may be running behind it. But the reason says
    # what to fix, not just that something failed.
    assert state.engine == container.UNKNOWN
    assert state.reason == "the docker engine socket is not accessible"


def test_an_engine_that_never_answers_is_given_up_on_within_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = Path(tempfile.mkdtemp(prefix="hd", dir="/tmp"))
    socket_path = directory / "hung.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)  # accepts the connection, never reads or replies
    monkeypatch.setenv("DOCKER_HOST", f"unix://{socket_path}")
    try:
        started = time.monotonic()
        state = container.detect(timeout=0.3)
        elapsed = time.monotonic() - started
    finally:
        listener.close()
        socket_path.unlink(missing_ok=True)
        directory.rmdir()
    assert state.engine == container.UNKNOWN
    assert state.reason == "the docker engine did not answer in time"
    assert elapsed < 2.0
    assert "/" not in str(state.reason)
