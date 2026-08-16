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
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from harrier.db import data_dir
from harrier.sources import scrub_secrets

PROTOCOL_PREFIX = "::harrier::"
# `interrupted` is not a failure. A run whose process disappeared, which is
# what a reloading development server does to every child it started, did not
# fail: nobody knows how it ended, and calling that failed is a claim the
# server cannot support (spec 041).
RunState = Literal["queued", "running", "succeeded", "failed", "cancelled", "interrupted"]
TERMINAL_STATES: frozenset[str] = frozenset({"succeeded", "failed", "cancelled", "interrupted"})

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


@dataclass(frozen=True)
class ParameterizedKind:
    """A run kind and the flags its CLI verb accepts.

    The kind owns the mapping to a CLI verb and to the flags that verb
    accepts, so no caller assembles argv (spec 047).

    Spec 047 gave this one boolean and a job. Spec 048's verbs need more:
    `find-contacts` takes a count, `backfill-posters` takes no job at all.
    Rather than a field per flag, a kind declares the closed sets it accepts.
    The property spec 047 asked for is unchanged and is now easier to state:
    a flag name reaching argv came from one of these sets, and a value
    reaching argv is an int or a path this process chose.
    """

    verb: str
    input_flag: str | None = None
    switches: frozenset[str] = frozenset()
    numbers: frozenset[str] = frozenset()
    takes_job: bool = True


PARAMETERIZED_KINDS: dict[str, ParameterizedKind] = {
    "tailor": ParameterizedKind("tailor", "--jd-file", switches=frozenset({"--no-ai"})),
    "cover-letter": ParameterizedKind("cover-letter", "--notes-file"),
    "answers": ParameterizedKind("answers", "--questions-file"),
    "evaluate": ParameterizedKind("evaluate", "--jd-file"),
    "find-contacts": ParameterizedKind(
        "find-contacts",
        switches=frozenset({"--best-only"}),
        numbers=frozenset({"--max-items"}),
    ),
    "outreach-draft": ParameterizedKind(
        "outreach-draft", "--input-file", switches=frozenset({"--ai"})
    ),
    "backfill-posters": ParameterizedKind(
        "backfill-posters",
        switches=frozenset({"--dry-run"}),
        numbers=frozenset({"--limit"}),
        takes_job=False,
    ),
    "gmail-watch": ParameterizedKind(
        "gmail-watch",
        switches=frozenset({"--dry-run"}),
        takes_job=False,
    ),
}


@dataclass(frozen=True)
class RunParams:
    """Validated inputs for a parameterized run.

    `job_id` is an int, not a string: the one selector that reaches argv
    cannot then be made to look like a flag. Operator free text never appears
    here at all. It goes to `input_path`, a file this process wrote, because
    argv is readable from the process table by every other process on the
    machine and application answers are exactly the content ADR-008 keeps out
    of reach (spec 047).

    A contact's name and LinkedIn URL travel the same way, for the same
    reason: they are a real person's details, and the process table is
    readable by everything else on the machine (spec 048).
    """

    job_id: int | None = None
    input_path: Path | None = None
    switches: frozenset[str] = frozenset()
    numbers: Mapping[str, int] = field(default_factory=dict[str, int])

    def __post_init__(self) -> None:
        if self.job_id is not None and self.job_id <= 0:
            raise ValueError(f"job id must be a positive integer, got {self.job_id!r}")
        for flag, value in self.numbers.items():
            # bool is a subclass of int, so a True here satisfies the type and
            # then renders as `--limit=True`, which the CLI rejects at a
            # distance with a message about the wrong thing.
            if isinstance(value, bool):
                raise ValueError(f"{flag} must be an integer, got {value!r}")


def run_inputs_dir() -> Path:
    return data_dir() / "runs" / "inputs"


def write_run_input(text: str) -> Path:
    """Put the operator's free text on disk so it never reaches argv.

    Owner-readable only: this is the operator's own words about a job
    application, and it lives under the data directory, which is
    never-in-git. It is removed when the run that consumes it ends
    (spec 047).
    """
    directory = run_inputs_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{uuid.uuid4().hex}.txt"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def build_command(kind: str, params: RunParams) -> list[str]:
    """The argv for a parameterized kind.

    Values are passed as `--flag=value` rather than as two arguments so that a
    value beginning with a dash is still a value. `job_id` cannot produce one,
    and the input path is a name this process chose, so neither can today;
    the form is used anyway because the guarantee should not depend on every
    future caller re-deriving that argument.
    """
    if kind not in PARAMETERIZED_KINDS:
        raise KeyError(kind)
    parameterized = PARAMETERIZED_KINDS[kind]
    argv = [sys.executable, "-m", "harrier_cli.main", parameterized.verb]

    if parameterized.takes_job:
        if params.job_id is None:
            raise ValueError(f"{kind} acts on a job and none was given")
        argv.append(f"--job-id={params.job_id}")
    elif params.job_id is not None:
        raise ValueError(f"{kind} acts on everything and takes no job")

    for flag in sorted(params.switches):
        if flag not in parameterized.switches:
            raise ValueError(f"{kind} does not accept {flag}")
        argv.append(flag)
    for flag in sorted(params.numbers):
        if flag not in parameterized.numbers:
            raise ValueError(f"{kind} does not accept {flag}")
        argv.append(f"{flag}={params.numbers[flag]}")

    if params.input_path is not None:
        if parameterized.input_flag is None:
            raise ValueError(f"{kind} takes no input file")
        argv.append(f"{parameterized.input_flag}={params.input_path}")
    return argv


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def scrub_event_data(data: dict[str, object]) -> dict[str, object]:
    """Every string a structured event carries, scrubbed.

    The log-line branches were scrubbed and this one was not, so a subprocess
    emitting a well-formed protocol object whose message or URL held a token
    put it straight onto the unauthenticated stream. Scrubbing only what I
    had just changed is the mistake; the property is that nothing reaches the
    stream unscrubbed (review finding on PR #39).
    """
    scrubbed: dict[str, object] = {}
    for key, value in data.items():
        if isinstance(value, str):
            scrubbed[key] = scrub_secrets(value)
        elif isinstance(value, dict):
            scrubbed[key] = scrub_event_data(cast("dict[str, object]", value))
        elif isinstance(value, list):
            scrubbed[key] = [
                scrub_secrets(item) if isinstance(item, str) else item
                for item in cast("list[object]", value)
            ]
        else:
            scrubbed[key] = value
    return scrubbed


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
    # What this run acts on, and what it therefore locks against. Empty for
    # the kinds that act on everything, which keeps their one-at-a-time
    # behaviour exactly as it was (spec 047).
    target: str = ""
    input_path: Path | None = None


class RunManager:
    """One active run per kind, per process.

    Per process is the honest scope and it used to be stated more strongly
    than it was enforced: the registry is in memory, so two workers hold two
    registries and the invariant holds within each rather than across the
    machine (spec 041).

    The deployment this is for runs a single uvicorn worker on the operator's
    own laptop, where per process and per machine are the same thing. Moving
    the registry into SQLite would make the stronger claim true and is not
    done here, because it would buy nothing for that deployment and the
    claim is now accurate as written. A second worker would need it.
    """

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

    def active_run(self, kind: str, target: str = "") -> Run | None:
        for run in self._runs.values():
            if run.kind == kind and run.target == target and run.state in ("queued", "running"):
                return run
        return None

    # -- lifecycle ----------------------------------------------------------

    async def start(self, kind: str, params: RunParams | None = None) -> Run:
        """Start a run, or return the already-active run for this target.

        The lock is per (kind, target) rather than per kind, so two jobs
        tailor at once while one job tailored twice joins the run already in
        flight. ADR-004 called for this under "artifact renders are per-slug
        locked"; spec 047 is where it was built.
        """
        # A kind that acts on everything locks on the empty target, which is
        # the one-at-a-time behaviour discovery always had. Kinds never
        # collide with each other because the lock is on the pair.
        target = "" if params is None or params.job_id is None else str(params.job_id)
        active = self.active_run(kind, target)
        if active is not None:
            # This attempt never becomes a run, so the input file written for
            # it has no terminal state to be cleaned up by. Removing it here
            # is the difference between joining a run and leaking a file of
            # the operator's own words on every double click.
            self._discard_input(params)
            return active
        command = list(self._kind_commands[kind]) if params is None else build_command(kind, params)
        run = Run(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            command=command,
            target=target,
            input_path=None if params is None else params.input_path,
        )
        self._runs[run.id] = run
        self._journal(run)
        self._tasks[run.id] = asyncio.create_task(self._execute(run))
        return run

    @staticmethod
    def _discard_input(params: RunParams | None) -> None:
        if params is None or params.input_path is None:
            return
        params.input_path.unlink(missing_ok=True)

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
        try:
            await self._run_process(run)
        finally:
            # Every terminal state, including a failed spawn and a
            # cancellation, and including an unexpected exception: the
            # operator's free text does not outlive the run that consumed it
            # (spec 047).
            self._cleanup_input(run)

    def _cleanup_input(self, run: Run) -> None:
        if run.input_path is None:
            return
        run.input_path.unlink(missing_ok=True)
        run.input_path = None

    async def _run_process(self, run: Run) -> None:
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
        """The one place an event reaches the stream, so the one place to scrub.

        Scrubbing at each call site meant scrubbing the sites I had just
        changed: the log-line branches were covered and the structured-event
        branch was not, and the test I wrote looked only at the branches I had
        covered (review finding on PR #39). Doing it here makes the property
        hold for every future caller without anyone remembering.
        """
        async with self._condition:
            run.events.append(
                RunEvent(id=len(run.events) + 1, type=event_type, data=scrub_event_data(data))
            )
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
            # Last record per id wins. A run left non-terminal belonged to a
            # process that is gone, and this one cannot know how it ended, so
            # it is `interrupted` rather than `failed`. Reporting it failed
            # was a guess dressed as a fact, and a reloading development
            # server produced one on every reload (spec 041).
            resolved: RunState = "interrupted"
            if state in TERMINAL_STATES:
                assert state in ("succeeded", "failed", "cancelled", "interrupted")
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
