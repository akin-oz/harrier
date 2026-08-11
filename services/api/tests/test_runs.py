"""Run manager behavior (spec 006)."""

import asyncio
import json
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


def test_journal_marks_orphaned_run_failed(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    journal.write_text(
        json.dumps({"id": "abc", "kind": "demo", "state": "running", "created_at": "x"}) + "\n",
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
