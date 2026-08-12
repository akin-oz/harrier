"""Run manager behavior (spec 006)."""

import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from conftest import auth
from fastapi.testclient import TestClient

from harrier_api.app import create_app
from harrier_api.runs import RunManager, format_sse

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

FAST_SCRIPT = (
    "import json; "
    'print("::harrier::" + json.dumps({"event": "progress", "step": 1, "total": 2}), flush=True); '
    'print("plain log line", flush=True); '
    'print("::harrier::" + json.dumps({"event": "progress", "step": 2, "total": 2}), flush=True)'
)
SLOW_SCRIPT = "import time; print('starting', flush=True); time.sleep(30)"
FAILING_SCRIPT = "import sys; print('boom', flush=True); sys.exit(3)"


def _manager(tmp_path: Path, script: str) -> RunManager:
    return RunManager(
        journal_path=tmp_path / "journal.jsonl",
        kind_commands={"demo": [sys.executable, "-c", script]},
        grace_seconds=0.5,
    )


def test_run_succeeds_with_parsed_events(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = _manager(tmp_path, FAST_SCRIPT)
        run = await manager.start("demo")
        finished = await manager.wait(run.id)
        assert finished.state == "succeeded"
        assert finished.exit_code == 0
        types = [event.type for event in finished.events]
        assert types.count("progress") == 2
        assert "log_line" in types
        assert types[-1] == "state_change"
        assert [event.id for event in finished.events] == list(range(1, len(finished.events) + 1))

    asyncio.run(scenario())


def test_failure_maps_to_failed_state(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = _manager(tmp_path, FAILING_SCRIPT)
        run = await manager.start("demo")
        finished = await manager.wait(run.id)
        assert finished.state == "failed"
        assert finished.exit_code == 3

    asyncio.run(scenario())


def test_cancel_terminates_and_marks_cancelled(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = _manager(tmp_path, SLOW_SCRIPT)
        run = await manager.start("demo")
        for _ in range(100):
            if run.events:
                break
            await asyncio.sleep(0.05)
        cancelled = await manager.cancel(run.id)
        assert cancelled is not None
        finished = await manager.wait(run.id)
        assert finished.state == "cancelled"

    asyncio.run(scenario())


def test_second_start_returns_active_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = _manager(tmp_path, SLOW_SCRIPT)
        first = await manager.start("demo")
        second = await manager.start("demo")
        assert first.id == second.id
        await manager.cancel(first.id)
        await manager.wait(first.id)

    asyncio.run(scenario())


def test_stream_replays_from_last_event_id(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = _manager(tmp_path, FAST_SCRIPT)
        run = await manager.start("demo")
        await manager.wait(run.id)
        all_events = [event.id async for event in manager.stream(run.id, 0)]
        assert all_events == [event.id for event in run.events]
        replayed = [event.id async for event in manager.stream(run.id, 2)]
        assert replayed == all_events[2:]

    asyncio.run(scenario())


def test_journal_survives_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = _manager(tmp_path, FAST_SCRIPT)
        run = await manager.start("demo")
        await manager.wait(run.id)
        return None

    asyncio.run(scenario())
    reloaded = RunManager(journal_path=tmp_path / "journal.jsonl", kind_commands={})
    runs = reloaded.list_runs()
    assert len(runs) == 1
    assert runs[0].state == "succeeded"


def test_journal_marks_an_orphaned_run_interrupted_not_failed(tmp_path: Path) -> None:
    """A run whose process is gone did not fail: nobody knows how it ended.

    Calling it failed was a guess dressed as a fact, and a reloading
    development server produced one on every reload, so the journal filled
    with failures that were only restarts (spec 041).
    """
    journal = tmp_path / "journal.jsonl"
    journal.write_text(
        json.dumps({"id": "abc", "kind": "demo", "state": "running", "created_at": "x"}) + "\n",
        encoding="utf-8",
    )
    reloaded = RunManager(journal_path=journal, kind_commands={})
    assert reloaded.list_runs()[0].state == "interrupted"


def test_an_interrupted_run_does_not_block_the_next_one(tmp_path: Path) -> None:
    """Interrupted is terminal. If it were not, the orphan left by a reload
    would be the active run forever and every start would return it."""
    journal = tmp_path / "journal.jsonl"
    journal.write_text(
        json.dumps({"id": "abc", "kind": "demo", "state": "running", "created_at": "x"}) + "\n",
        encoding="utf-8",
    )
    reloaded = RunManager(journal_path=journal, kind_commands={})
    assert reloaded.active_run("demo") is None


def test_a_real_failure_is_still_recorded_as_failed(tmp_path: Path) -> None:
    """The distinction only means something if failed still happens."""
    journal = tmp_path / "journal.jsonl"
    journal.write_text(
        json.dumps({"id": "abc", "kind": "demo", "state": "failed", "created_at": "x"}) + "\n",
        encoding="utf-8",
    )
    reloaded = RunManager(journal_path=journal, kind_commands={})
    assert reloaded.list_runs()[0].state == "failed"


def test_format_sse_shape(tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = _manager(tmp_path, FAST_SCRIPT)
        run = await manager.start("demo")
        await manager.wait(run.id)
        # events[0] is the state_change to running; take the first progress.
        first_progress = next(event for event in run.events if event.type == "progress")
        rendered = format_sse(first_progress)
        assert rendered.startswith(f"id: {first_progress.id}\ndata: ")
        assert rendered.endswith("\n\n")
        payload = json.loads(rendered.split("data: ", 1)[1])
        assert payload["type"] == "progress"
        assert payload["step"] == 1

    asyncio.run(scenario())


def test_endpoints_start_poll_cancel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    manager = _manager(tmp_path, SLOW_SCRIPT)
    # Context manager keeps one portal (and so the background run task) alive
    # across requests; without it every request gets its own event loop.
    with TestClient(create_app(run_manager=manager)) as client:
        started = client.post("/runs", json={"kind": "demo"}, headers=auth())
        assert started.status_code == 200
        run_id = started.json()["id"]

        listed = client.get("/runs").json()
        assert [run["id"] for run in listed] == [run_id]

        cancelled = client.post(f"/runs/{run_id}/cancel", headers=auth())
        assert cancelled.status_code == 200
        deadline = time.monotonic() + 5
        state = ""
        while time.monotonic() < deadline:
            state = client.get(f"/runs/{run_id}").json()["state"]
            if state == "cancelled":
                break
            time.sleep(0.05)
        assert state == "cancelled"
        assert client.get("/runs/nope").status_code == 404


def test_run_event_payload_is_in_the_contract() -> None:
    """Spec 006 review follow-up: SSE payload shape lives in the OpenAPI schema."""
    schema = create_app().openapi()
    assert "RunEventOut" in schema["components"]["schemas"]
    properties = schema["components"]["schemas"]["RunEventOut"]["properties"]
    assert set(properties) >= {"type", "line", "step", "total", "message", "state", "exit_code"}


def test_a_database_already_at_the_previous_version_gains_the_job_runs_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`job_runs` is its own migration, not an addition to the last one.

    Spec 033's migration 3 has already shipped, and the runner skips any
    version at or below the recorded one. Appending this table to migration 3
    would mean every database that had already applied it never received the
    table, and the failure would be a missing-table error at the first
    scheduled run rather than anything visible here (conflict resolution
    between specs 029 and 033).
    """
    from harrier.db import connect
    from harrier.tracker.schema import MIGRATIONS

    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)

    # A database built up to version 3 and stopped there, as a real one would
    # have been before this branch.
    path = tmp_path / "data"
    path.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(path / "tracker.db")
    old.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    for version, statements in MIGRATIONS:
        if version > 3:
            continue
        for statement in statements:
            old.execute(statement)
        old.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
    old.commit()
    # Read directly: `schema_version` expects the application's row factory.
    assert old.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 3
    tables = {row[0] for row in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "job_runs" not in tables
    old.close()

    # Opening it through the application brings it forward.
    conn = connect()
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "job_runs" in tables
    conn.close()
