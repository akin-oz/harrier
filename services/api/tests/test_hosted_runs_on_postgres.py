"""Hosted runs on Postgres (spec 106): the run's session, its records, the
run login's start check and the stop signal.

The hosted app is built as uvicorn builds it, on a copy of the migrated
template, connecting as a login role made for the test and starting runs
with a second one, the run login. Runs are real processes whose commands
the tests choose. Owners, keys and credentials are made at run time and
synthetic; no test reaches the network. Every role a test makes is dropped
after it.
"""

# Pyright strict cannot resolve starlette's TestClient request and response
# members, which is why every API test file carries these.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from collections.abc import Generator, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from pg_support import open_owner_store
from test_hosted_auth import (
    API_HOST,
    bearer,
    claims_for,
    credentials_key,
    mint,
    new_credentials_key_text,
)
from test_hosted_logs import fresh_logging  # noqa: F401  # pyright: ignore[reportUnusedImport]
from test_hosted_requests import (
    Server,
    copied_server,
    hosted_environment,
    keys_served_here,
    run_login_for,
)
from test_owner_policy import fill

from harrier import logsetup
from harrier.connection import RUN_OWNER_VARIABLE, URL_VARIABLE
from harrier.credentials import parse_key, put_credential
from harrier.pgstore import postgres_connect
from harrier.runoutcome import last_success, record_success
from harrier.tracker.schema import OWNED_TABLES
from harrier_api.app import create_app
from harrier_api.hostedauth import RUN_DATABASE_URL_REFUSAL, HostedConfigError
from harrier_api.hostedstore import RUN_LOGIN_IS_THE_API_LOGIN, HostedStartRefused
from harrier_api.runs import TERMINAL_STATES, RunManager


@pytest.fixture(name="server")
def server_fixture(postgres_template: str) -> Iterator[Server]:
    yield from copied_server(postgres_template)


@dataclass
class App:
    client: TestClient
    manager: RunManager
    key: ec.EllipticCurvePrivateKey
    server: Server

    def headers(self, owner: str) -> dict[str, str]:
        return bearer(mint(self.key, claims_for(sub=owner)))

    def start(self, owner: str) -> str:
        started = self.client.post("/runs", json={"kind": "demo"}, headers=self.headers(owner))
        assert started.status_code == 200, started.text
        return str(started.json()["id"])

    def ended(self, run_id: str) -> Any:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            run = self.manager.get(run_id)
            if run is not None and run.state in TERMINAL_STATES:
                return run
            time.sleep(0.05)
        raise AssertionError(f"run {run_id} did not end")

    def row(self, run_id: str) -> dict[str, Any] | None:
        """The run's `run_records` row, as the migrating role reads it."""
        with closing(postgres_connect(self.server.url)) as conn:
            found = conn.execute(
                "SELECT owner_id::text, state, api_instance, started_at, ended_at, exit_code "
                "FROM run_records WHERE run_id = %s",
                (run_id,),
            ).fetchone()
        if found is None:
            return None
        keys = ("owner", "state", "api_instance", "started_at", "ended_at", "exit_code")
        return dict(zip(keys, found, strict=True))


def waits_for(signal: Path) -> str:
    """A child that waits for `signal` to exist, at most 20 seconds, so a
    failed test leaves no process behind."""
    return (
        "import os, time\n"
        "deadline = time.monotonic() + 20\n"
        f"while not os.path.exists({str(signal)!r}) and time.monotonic() < deadline:\n"
        "    time.sleep(0.02)\n"
    )


def demo_manager(tmp_path: Path, script: str) -> RunManager:
    return RunManager(
        journal_path=tmp_path / "journal.jsonl",
        kind_commands={"demo": [sys.executable, "-c", script]},
        grace_seconds=0.5,
    )


@contextmanager
def hosted_app(
    monkeypatch: pytest.MonkeyPatch, server: Server, manager: RunManager
) -> Generator[App]:
    login = server.login_role()
    with run_login_for(login.url) as run_url:
        key = keys_served_here(monkeypatch)
        hosted_environment(monkeypatch, login.url, run_url)
        app = create_app(run_manager=manager)
        with TestClient(app, base_url=f"http://{API_HOST}") as client:
            yield App(client, manager, key, server)


def wait_until(condition: Any, what: str) -> None:
    deadline = time.monotonic() + 30
    while not condition():
        assert time.monotonic() < deadline, what
        time.sleep(0.05)


# --- the run's database session ---

PRINT_OWNERS = (
    "import json, os\n"
    "from harrier.connection import open_store\n"
    "conn = open_store(owner=os.environ['HARRIER_RUN_OWNER'])\n"
    f"tables = {list(OWNED_TABLES)!r}\n"
    "seen = {t: sorted(str(r[0]) for r in conn.execute("
    "f'SELECT DISTINCT owner_id FROM {t}').fetchall()) for t in tables}\n"
    "role = conn.execute('SELECT current_user').fetchone()[0]\n"
    "print(json.dumps({'seen': seen, 'role': role}), flush=True)\n"
)


def test_a_run_reads_only_its_owners_rows(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    a, b = server.owner(), server.owner()
    with psycopg.connect(server.url, autocommit=True) as conn:
        fill(conn, a)
        fill(conn, b)
    # `fill` stores synthetic bytes as a credential, which does not decrypt
    # and would refuse A's run (spec 106): A's is replaced by a real one.
    with closing(open_owner_store(server.url, a)) as owned:
        put_credential(owned, credentials_key(), a, "apify", {"token": "synthetic"})
    with hosted_app(monkeypatch, server, demo_manager(tmp_path, PRINT_OWNERS)) as app:
        run = app.ended(app.start(a))
        assert run.state == "succeeded", [event.data for event in run.events]
    line = next(
        event.data["line"]
        for event in run.events
        if isinstance(event.data.get("line"), str) and event.data["line"].startswith("{")
    )
    printed = json.loads(line)
    assert printed["role"] == "harrier_tenant"
    assert printed["seen"] == {table: [a] for table in OWNED_TABLES}


def test_a_run_outcome_key_is_per_owner_without_naming_one(server: Server) -> None:
    a, b = server.owner(), server.owner()
    times = {a: "2026-10-12 08:00:00", b: "2026-10-13 09:00:00"}
    for owner, at in times.items():
        with closing(open_owner_store(server.url, owner)) as conn:
            record_success(conn, "discovery:1", at=at)
    for owner, at in times.items():
        with closing(open_owner_store(server.url, owner)) as conn:
            assert last_success(conn, "discovery:1") == at
    with closing(postgres_connect(server.url)) as conn:
        rows = conn.execute(
            "SELECT owner_id::text, last_success_at FROM job_runs WHERE job = 'discovery:1'"
        ).fetchall()
    assert sorted(rows) == sorted(times.items())


# --- log redaction ---


def test_a_hosted_run_redacts_logs_from_its_own_store(
    server: Server,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fresh_logging: None,  # noqa: F811
) -> None:
    a, b = server.owner(), server.owner()
    mine, theirs = (f"Synthetic Person {uuid.uuid4().hex[:8]}" for _ in range(2))
    for owner, name in ((a, mine), (b, theirs)):
        with closing(open_owner_store(server.url, owner)) as conn:
            conn.execute("INSERT INTO contacts (person_name) VALUES (?)", (name,))
    data = tmp_path / "run-data"
    monkeypatch.setenv("HARRIER_DATA_DIR", str(data))
    monkeypatch.setenv(URL_VARIABLE, server.login_role().url)
    monkeypatch.setenv(RUN_OWNER_VARIABLE, a)

    logsetup.configure_logging(force=True)
    logging.getLogger("synthetic").warning("found %s and %s", mine, theirs)
    for handler in logging.getLogger().handlers:
        handler.flush()
    written = logsetup.log_path().read_text(encoding="utf-8")
    assert mine not in written
    assert theirs in written
    assert "identity redaction is unavailable" not in written
    # The values came from Postgres: no SQLite file exists.
    assert not list(data.rglob("*.db"))


# --- the run login, at start ---


def test_hosted_start_checks_the_run_login_role(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    login = server.login_role()
    keys_served_here(monkeypatch)

    # Missing, empty, or not a Postgres URL: refused by the configuration.
    hosted_environment(monkeypatch, login.url)
    for value in (None, "", "sqlite:///tracker.db", "postgres://x:y@127.0.0.1/db"):
        if value is None:
            monkeypatch.delenv("HARRIER_RUN_DATABASE_URL")
        else:
            monkeypatch.setenv("HARRIER_RUN_DATABASE_URL", value)
        with pytest.raises(HostedConfigError) as refused:
            create_app()
        assert str(refused.value) == RUN_DATABASE_URL_REFUSAL

    # A role that fails a login role check, named by the variable and the
    # check, never by its URL or password.
    for attributes, tenant, check in (
        ("BYPASSRLS", "", "bypassrls"),
        ("", "none", "cannot set role harrier_tenant"),
    ):
        run = server.login_role(attributes, tenant=tenant)
        hosted_environment(monkeypatch, login.url, run.url)
        with pytest.raises(HostedStartRefused) as refused:
            create_app()
        text = str(refused.value)
        assert "HARRIER_RUN_DATABASE_URL" in text and check in text, text
        assert run.password not in text and login.password not in text

    # The API's own role.
    hosted_environment(monkeypatch, login.url, login.url)
    with pytest.raises(HostedStartRefused) as refused:
        create_app()
    assert str(refused.value) == RUN_LOGIN_IS_THE_API_LOGIN

    # A run login of its own: the start goes on.
    run = server.login_role()
    hosted_environment(monkeypatch, login.url, run.url)
    app = create_app()
    app.state.request_store.close()


# --- credentials at the route ---


def test_a_route_refuses_a_run_whose_credential_is_missing_or_unreadable(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    owner = server.owner()
    with hosted_app(monkeypatch, server, demo_manager(tmp_path, "")) as app:
        headers = app.headers(owner)
        missing = app.client.post("/ops/evaluate-prospects", json={}, headers=headers)
        assert missing.status_code == 409, missing.text
        assert missing.json() == {
            "detail": "this run needs your ai credential; set it under credentials"
        }
        sends = app.client.post("/ops/digest", json={"dry_run": False}, headers=headers)
        assert sends.status_code == 409, sends.text
        assert "telegram" in sends.json()["detail"]

        # Written with another key: it does not decrypt with the API's.
        other = parse_key(new_credentials_key_text())
        assert other is not None
        with closing(open_owner_store(server.url, owner)) as conn:
            put_credential(conn, other, owner, "ai", {"provider": "openai-api", "key": "k"})
        unreadable = app.client.post("/ops/evaluate-prospects", json={}, headers=headers)
        assert unreadable.status_code == 409, unreadable.text
        assert unreadable.json() == {"detail": "your ai credential cannot be read; set it again"}

        # Nothing was recorded, and nothing runs.
        assert app.manager.list_runs() == []
    with closing(postgres_connect(server.url)) as conn:
        assert conn.execute("SELECT count(*) FROM run_records").fetchone() == (0,)


# --- run records ---


def test_a_run_cut_off_by_a_stop_reads_interrupted(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    signal = tmp_path / "go-on"
    script = waits_for(signal)
    a, b = server.owner(), server.owner()
    # Two rows an earlier API process left running, one per owner.
    left = {a: uuid.uuid4().hex[:12], b: uuid.uuid4().hex[:12]}
    with psycopg.connect(server.url, autocommit=True) as conn:
        for owner, run_id in left.items():
            conn.execute(
                "INSERT INTO run_records (owner_id, run_id, kind, target, state, api_instance, "
                "created_at, started_at) VALUES (%s, %s, 'demo', '', 'running', %s, %s, %s)",
                (owner, run_id, "an-earlier-process", "2026-10-12T09:00:00+00:00", "2026-10-12"),
            )

    with hosted_app(monkeypatch, server, demo_manager(tmp_path, script)) as app:
        records = app.manager.hosted_runs
        assert records is not None
        # A run's row is inserted at start and follows its state as its owner.
        live = app.start(a)
        wait_until(lambda: (app.row(live) or {}).get("state") == "running", "running")
        row = app.row(live)
        assert row is not None
        assert row["owner"] == a and row["api_instance"] == records.api_instance
        assert row["started_at"] is not None
        signal.write_text("", encoding="utf-8")
        app.ended(live)
        wait_until(lambda: (app.row(live) or {}).get("state") == "succeeded", "succeeded")
        ended = app.row(live)
        assert ended is not None
        assert ended["exit_code"] == 0 and ended["ended_at"] is not None

        # A's list shows the cut-off row interrupted, and stores it so.
        listed = app.client.get("/runs", headers=app.headers(a))
        assert listed.status_code == 200, listed.text
        states = {item["id"]: item["state"] for item in listed.json()}
        assert states == {live: "succeeded", left[a]: "interrupted"}
        stored = app.row(left[a])
        assert stored is not None
        assert stored["state"] == "interrupted" and stored["ended_at"] is not None
        # B's row is untouched until B reads it, and A cannot read it.
        assert (app.row(left[b]) or {})["state"] == "running"
        assert app.client.get(f"/runs/{left[b]}", headers=app.headers(a)).status_code == 404
        theirs = app.client.get(f"/runs/{left[b]}", headers=app.headers(b))
        assert theirs.status_code == 200 and theirs.json()["state"] == "interrupted"
        # A run not live here has no events: an unknown id.
        events = app.client.get(f"/runs/{left[a]}/events", headers=app.headers(a))
        assert events.status_code == 404


def test_the_stop_signal_records_active_runs_interrupted(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = "import time\nprint('started', flush=True)\ntime.sleep(60)\n"
    a, b = server.owner(), server.owner()
    manager = demo_manager(tmp_path, script)
    with hosted_app(monkeypatch, server, manager) as app:
        runs = [app.start(owner) for owner in (a, b)]
        for run_id in runs:
            wait_until(lambda run_id=run_id: (app.row(run_id) or {}).get("state") == "running", "r")
    # Leaving the client is the app's shutdown, which uvicorn runs on the
    # signal a machine stop sends.
    for run_id in runs:
        row = app.row(run_id)
        assert row is not None
        assert row["state"] == "interrupted" and row["ended_at"] is not None
        held = manager.get(run_id)
        assert held is not None and held.state == "interrupted"
    assert all(
        process.returncode is not None
        for process in manager._processes.values()  # pyright: ignore[reportPrivateUsage]
    )
