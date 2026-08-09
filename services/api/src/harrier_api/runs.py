"""Run manager: subprocess execution with live events (spec 006, ADR-004).

Runs execute harrier CLI entry points as child processes. Stdout is parsed
line by line: lines starting with the protocol prefix carry structured JSON
events, everything else is a log line. Events get monotonically increasing
ids per run so SSE reconnects can replay from Last-Event-ID.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from harrier.db import data_dir

PROTOCOL_PREFIX = "::harrier::"
RunState = Literal["queued", "running", "succeeded", "failed", "cancelled"]
TERMINAL_STATES: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})

# Run kinds and their commands.
KIND_COMMANDS: dict[str, list[str]] = {
    "discovery": [sys.executable, "-m", "harrier_cli.main", "discover"],
    "demo": [
        sys.executable,
        "-m",
        "harrier_cli.main",
        "demo-run",
        "--steps",
        "8",
        "--delay",
        "0.4",
    ],
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class RunEvent:
    id: int
    type: str
    data: dict[str, object]


@dataclass
class Run:
    id: str
    kind: str
    command: list[str]
    state: RunState = "queued"
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    ended_at: str | None = None
    exit_code: int | None = None
    events: list[RunEvent] = field(default_factory=list[RunEvent])
    cancel_requested: bool = False


class RunManager:
    def __init__(
        self,
        journal_path: Path | None = None,
        kind_commands: dict[str, list[str]] | None = None,
        grace_seconds: float = 5.0,
    ) -> None:
        self._journal_path = (
            journal_path if journal_path is not None else data_dir() / "runs" / "journal.jsonl"
        )
        self._kind_commands = kind_commands if kind_commands is not None else KIND_COMMANDS
        self._grace_seconds = grace_seconds
        self._runs: dict[str, Run] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._condition = asyncio.Condition()
        self._load_journal()

    # -- queries ------------------------------------------------------------

    def kinds(self) -> list[str]:
        return sorted(self._kind_commands)

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def list_runs(self) -> list[Run]:
        return sorted(self._runs.values(), key=lambda run: run.created_at, reverse=True)

    def active_run(self, kind: str) -> Run | None:
        for run in self._runs.values():
            if run.kind == kind and run.state in ("queued", "running"):
                return run
        return None

    # -- lifecycle ----------------------------------------------------------

    async def start(self, kind: str) -> Run:
        """Start a run, or return the already-active run of this kind (ADR-004)."""
        active = self.active_run(kind)
        if active is not None:
            return active
        command = self._kind_commands[kind]
        run = Run(id=uuid.uuid4().hex[:12], kind=kind, command=list(command))
        self._runs[run.id] = run
        self._journal(run)
        self._tasks[run.id] = asyncio.create_task(self._execute(run))
        return run

    async def cancel(self, run_id: str) -> Run | None:
        """Request cancellation and return immediately; the state change lands
        when the process is reaped. SIGKILL escalation runs in the background
        after the grace period (ADR-004)."""
        run = self._runs.get(run_id)
        if run is None or run.state in TERMINAL_STATES:
            return run
        run.cancel_requested = True
        process = self._processes.get(run_id)
        if process is not None and process.returncode is None:
            process.terminate()
            self._tasks[f"{run_id}:escalate"] = asyncio.create_task(self._escalate(run_id, process))
        return self._runs.get(run_id)

    async def _escalate(self, run_id: str, process: asyncio.subprocess.Process) -> None:
        task = self._tasks.get(run_id)
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self._grace_seconds)
        except TimeoutError:
            if process.returncode is None:
                process.kill()

    async def wait(self, run_id: str) -> Run:
        task = self._tasks.get(run_id)
        if task is not None:
            await task
        run = self._runs[run_id]
        return run

    async def _execute(self, run: Run) -> None:
        await self._set_state(run, "running")
        run.started_at = _now()
        try:
            process = await asyncio.create_subprocess_exec(
                *run.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as error:
            await self._append(run, "log_line", {"line": f"failed to spawn: {error}"})
            await self._set_state(run, "failed")
            run.ended_at = _now()
            return
        self._processes[run.id] = process
        stdout = process.stdout
        assert stdout is not None
        async for raw in stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if line.startswith(PROTOCOL_PREFIX):
                payload = line[len(PROTOCOL_PREFIX) :]
                try:
                    parsed_raw: object = json.loads(payload)
                except json.JSONDecodeError:
                    await self._append(run, "log_line", {"line": line})
                    continue
                if isinstance(parsed_raw, dict):
                    # JSON object keys are always strings; the cast states that.
                    data = dict(cast("dict[str, object]", parsed_raw))
                    event_type = str(data.pop("event", "progress"))
                    await self._append(run, event_type, data)
                else:
                    await self._append(run, "log_line", {"line": line})
            else:
                await self._append(run, "log_line", {"line": line})
        run.exit_code = await process.wait()
        run.ended_at = _now()
        if run.cancel_requested:
            await self._set_state(run, "cancelled")
        elif run.exit_code == 0:
            await self._set_state(run, "succeeded")
        else:
            await self._set_state(run, "failed")

    # -- events -------------------------------------------------------------

    async def _append(self, run: Run, event_type: str, data: dict[str, object]) -> None:
        async with self._condition:
            run.events.append(RunEvent(id=len(run.events) + 1, type=event_type, data=data))
            self._condition.notify_all()

    async def _set_state(self, run: Run, state: RunState) -> None:
        run.state = state
        self._journal(run)
        await self._append(run, "state_change", {"state": state, "exit_code": run.exit_code})

    async def stream(self, run_id: str, last_event_id: int = 0) -> AsyncIterator[RunEvent]:
        """Yield events after last_event_id, live until the run is terminal."""
        run = self._runs[run_id]
        index = last_event_id
        while True:
            async with self._condition:
                while len(run.events) <= index and run.state not in TERMINAL_STATES:
                    await self._condition.wait()
                pending = run.events[index:]
            for event in pending:
                index = event.id
                yield event
            if run.state in TERMINAL_STATES and index >= len(run.events):
                return

    # -- journal ------------------------------------------------------------

    def _journal(self, run: Run) -> None:
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "id": run.id,
            "kind": run.kind,
            "state": run.state,
            "created_at": run.created_at,
            "started_at": run.started_at,
            "ended_at": run.ended_at,
            "exit_code": run.exit_code,
        }
        with self._journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def _load_journal(self) -> None:
        """Rebuild terminal runs from the journal so restarts can list history."""
        if not self._journal_path.is_file():
            return
        for line in self._journal_path.read_text(encoding="utf-8").splitlines():
            try:
                parsed: object = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            # JSON object keys are always strings; the cast states that.
            record = cast(dict[str, object], parsed)
            state = str(record.get("state", ""))
            run_id = str(record.get("id", ""))
            if not run_id:
                continue
            # Last record per id wins; a run left non-terminal by a dead
            # server is reported failed rather than forever running.
            resolved: RunState = "failed"
            if state in TERMINAL_STATES:
                assert state in ("succeeded", "failed", "cancelled")
                resolved = state
            exit_code_raw = record.get("exit_code")
            self._runs[run_id] = Run(
                id=run_id,
                kind=str(record.get("kind", "")),
                command=[],
                state=resolved,
                created_at=str(record.get("created_at", "")),
                started_at=(str(record["started_at"]) if record.get("started_at") else None),
                ended_at=(str(record["ended_at"]) if record.get("ended_at") else None),
                exit_code=(int(str(exit_code_raw)) if exit_code_raw is not None else None),
            )


def format_sse(event: RunEvent) -> str:
    payload = json.dumps({"type": event.type, **event.data}, sort_keys=True)
    return f"id: {event.id}\ndata: {payload}\n\n"
