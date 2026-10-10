"""Run manager: subprocess execution with live events (spec 006, ADR-004).

Runs execute harrier CLI entry points as child processes. Stdout is parsed
line by line: lines starting with the protocol prefix carry structured JSON
events, everything else is a log line. Events get monotonically increasing
ids per run so SSE reconnects can replay from Last-Event-ID.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import sys
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, cast

from harrier.db import data_dir
from harrier.discovery import SOURCE_ORDER
from harrier.sources import scrub_secrets
from harrier.tracks import InvalidSlugError, validate_slug

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

    Spec 050 adds two more closed sets. `choices` maps a flag to the values
    it may take, so a value reaching argv for it is one of a fixed set
    (`reconsider --source`). `dates` names flags whose value is a calendar
    date, rendered by this module (`digest --date`). The verb may be two
    words, a command and its subcommand (`config check-feeds`).

    Spec 095 adds `fractions`, flags whose value is a number between 0 and 1
    (`evaluate-prospects --threshold`), and `input_flags`, the flags that take
    one of several files this process wrote (`discover --dataset-file` and
    its two siblings). `input_flag` stays the one file a kind's free text
    goes to.
    """

    verb: str
    input_flag: str | None = None
    switches: frozenset[str] = frozenset()
    numbers: frozenset[str] = frozenset()
    takes_job: bool = True
    choices: Mapping[str, frozenset[str]] = field(default_factory=dict[str, frozenset[str]])
    dates: frozenset[str] = frozenset()
    fractions: frozenset[str] = frozenset()
    input_flags: frozenset[str] = frozenset()
    # A directory this process chose, never one a request named: where
    # `backup` writes (spec 096).
    destination_flag: str | None = None
    # An archive this process found in its own listing, passed as the verb's
    # one positional argument (spec 096).
    takes_archive: bool = False


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
    # The Operations page (spec 050).
    "check-feeds": ParameterizedKind(
        "config check-feeds",
        switches=frozenset({"--prune"}),
        takes_job=False,
    ),
    "reconsider": ParameterizedKind(
        "reconsider",
        switches=frozenset({"--apply"}),
        choices={"--source": frozenset(SOURCE_ORDER)},
        takes_job=False,
    ),
    # The browser's backup (spec 050) writes where the Settings page lists
    # (spec 096): the server passes the directory it reads.
    "backup": ParameterizedKind(
        "backup",
        switches=frozenset({"--no-prune"}),
        takes_job=False,
        destination_flag="--dest",
    ),
    "digest": ParameterizedKind(
        "digest",
        switches=frozenset({"--dry-run"}),
        dates=frozenset({"--date"}),
        takes_job=False,
    ),
    # Commands that work on the operator's data (spec 095).
    "events-backfill": ParameterizedKind(
        "events backfill",
        switches=frozenset({"--dry-run"}),
        takes_job=False,
    ),
    "evaluate-prospects": ParameterizedKind(
        "evaluate-prospects",
        switches=frozenset({"--apply", "--refresh", "--include-borderline"}),
        numbers=frozenset({"--limit"}),
        fractions=frozenset({"--threshold"}),
        takes_job=False,
    ),
    "scoring-export": ParameterizedKind("scoring export", takes_job=False),
    # `KIND_COMMANDS["discovery"]` stays the no-option form `POST /runs`
    # starts. Both lock on the same kind, so one discovery runs at a time.
    "discovery": ParameterizedKind(
        "discover",
        switches=frozenset({"--dry-run", "--no-notify", "--shadow"}),
        numbers=frozenset({"--apify-count"}),
        choices={"--only-source": frozenset(SOURCE_ORDER)},
        input_flags=frozenset({"--dataset-file", "--wellfound-file", "--wttj-file"}),
        takes_job=False,
    ),
    # Spec 096: an archive from the Settings page's listing.
    "verify-backup": ParameterizedKind("verify-backup", takes_job=False, takes_archive=True),
}


# A character that can continue a path component, so a directory's path is
# matched only where neither side continues it.
_PATH_CHAR = r"[\w.~-]"


@dataclass(frozen=True)
class HiddenDirectory:
    """A directory whose path never reaches the run's event stream.

    The commands a run executes print the paths they wrote, and on the host
    those carry the home directory. Within a run that names one of these, a
    file inside the directory is shown by its name and the directory itself
    by `shown_as`, so the archive a backup wrote reaches the browser as a
    name and never as a path (spec 096).
    """

    path: Path
    shown_as: str

    def hide(self, text: str) -> str:
        """The text with this directory's path removed, matched only as a
        whole path: `/app/database.old` beside `/app/data` is another path
        and is left alone, where a plain substring match turned it into
        "the data directorybase.old" (review of PR #214)."""
        root = str(self.path).rstrip(os.sep)
        if not root:
            return text
        whole = rf"(?<!{_PATH_CHAR}){re.escape(root)}"
        inside = re.sub(whole + re.escape(os.sep), "", text)
        return re.sub(
            rf"{whole}(?![\w~-]|\.{_PATH_CHAR}|{re.escape(os.sep)})",
            lambda _: self.shown_as,
            inside,
        )


def backup_hidden_directories() -> tuple[HiddenDirectory, ...]:
    """The directories a backup or a verification must never show by path:
    the archive is named, never located (spec 096)."""
    from harrier.backup import backup_dir

    return (
        HiddenDirectory(backup_dir(), "the backups directory"),
        HiddenDirectory(data_dir(), "the data directory"),
    )


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
    # The track the run works in, by slug (spec 093). Checked against the slug
    # rule here; the route that sets it checks the slug exists.
    track: str | None = None
    choices: Mapping[str, str] = field(default_factory=dict[str, str])
    dates: Mapping[str, date] = field(default_factory=dict[str, date])
    # What the run locks against, for a kind that takes no job (spec 050).
    # It never reaches argv. Two attempts with the same target and the same
    # options join one run; other options are refused while it is active.
    # A dry digest and a sending one for the same day take different
    # targets because a preview writes nothing and may run beside a send.
    # A kind whose modes both act on shared state (discovery, evaluation,
    # backfill, reconsideration, feeds) takes one target per kind or track,
    # so its report and its write never run at once (review of PR #208).
    target: str | None = None
    fractions: Mapping[str, float] = field(default_factory=dict[str, float])
    # Files this process wrote, by the flag that passes each (spec 095).
    input_files: Mapping[str, Path] = field(default_factory=dict[str, Path])
    # Both chosen by the server, never by a request (spec 096).
    destination: Path | None = None
    archive: Path | None = None
    hidden: tuple[HiddenDirectory, ...] = ()

    def __post_init__(self) -> None:
        if self.track is not None:
            try:
                validate_slug(self.track)
            except InvalidSlugError as error:
                raise ValueError(str(error)) from error
        if self.job_id is not None and self.job_id <= 0:
            raise ValueError(f"job id must be a positive integer, got {self.job_id!r}")
        for flag, value in self.numbers.items():
            # bool is a subclass of int, so a True here satisfies the type and
            # then renders as `--limit=True`, which the CLI rejects at a
            # distance with a message about the wrong thing.
            if isinstance(value, bool):
                raise ValueError(f"{flag} must be an integer, got {value!r}")
        for flag, value in self.dates.items():
            # A datetime is a date, and would render with its time attached.
            if type(value) is not date:
                raise ValueError(f"{flag} must be a calendar date, got {value!r}")
        for flag, value in self.fractions.items():
            if isinstance(value, bool) or not math.isfinite(value):
                raise ValueError(f"{flag} must be a finite number, got {value!r}")
            if not 0 <= value <= 1:
                raise ValueError(f"{flag} must be between 0 and 1, got {value!r}")

    @property
    def files(self) -> tuple[Path, ...]:
        """Every file this attempt wrote, so none outlives it (spec 095)."""
        single = () if self.input_path is None else (self.input_path,)
        return (*single, *self.input_files.values())


def run_inputs_dir() -> Path:
    return data_dir() / "runs" / "inputs"


def write_run_input(text: str | bytes, suffix: str = ".txt") -> Path:
    """Put the operator's free text on disk so it never reaches argv.

    Bytes are written as they arrived: an uploaded export is the operator's
    file, not text this process decodes (spec 095).

    Owner-readable only, and owner-readable from the moment it exists. The
    first version wrote the file and then chmodded it, which left it at the
    umask's mode in between: on a common umask of 022 that is 0644, so the
    operator's words about a job application were readable by every other
    local user for the width of that window. The mode is now part of
    creation, and the directory is created private too, so the window has no
    path to it either (review finding on PR #51).

    It is removed when the run that consumes it ends (spec 047).
    """
    directory = run_inputs_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # mkdir's mode is ignored when the directory already exists, and this one
    # may predate the fix.
    directory.chmod(0o700)
    path = directory / f"{uuid.uuid4().hex}{suffix}"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    data = text if isinstance(text, bytes) else text.encode("utf-8")
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    return path


class RunConflictError(Exception):
    """A run of this kind is active for this target, with other options.

    Joining it would drop what this request asked for, and its uploads with
    it: a dry run would join a real one, a report would show another
    report's numbers (review of PR #208). The API answers 409 in these
    words; the attempt's files are already removed when this is raised.
    """

    def __init__(self, active: Run) -> None:
        self.active = active
        super().__init__(
            f"a {active.kind} run is already active (run {active.id}) with other options; "
            "wait for it to end or cancel it, then start this one"
        )


def _in_inputs(path: Path) -> bool:
    """Whether a path is a run input this module wrote, so nothing read back
    from the journal can name a file elsewhere for removal."""
    return path.parent == run_inputs_dir() and path.name != ""


def _signature(command: list[str], files: tuple[Path, ...]) -> tuple[str, ...]:
    """The argv with each input file's path replaced by a hash of its bytes.

    Two attempts are the same request when their options are the same and
    their files hold the same bytes: a double click joins, a changed option
    or a different upload does not. Paths alone cannot say so, because each
    attempt writes its own uniquely named copy.
    """
    digests = {str(path): _digest(path) for path in files}
    signature: list[str] = []
    for argument in command:
        flag, separator, value = argument.partition("=")
        if separator and value in digests:
            signature.append(f"{flag}=sha256:{digests[value]}")
        else:
            signature.append(argument)
    return tuple(signature)


def _digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        # Unreadable is never equal to anything, so it never joins.
        return f"unreadable:{uuid.uuid4().hex}"


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
    argv = [sys.executable, "-m", "harrier_cli.main"]
    if params.track is not None:
        # A global flag, so it goes before the verb (spec 093). The value is a
        # validated slug, in the `--flag=value` form this module uses for
        # every value.
        argv.append(f"--track={params.track}")
    argv.extend(parameterized.verb.split())

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
    for flag in sorted(params.choices):
        allowed = parameterized.choices.get(flag)
        if allowed is None:
            raise ValueError(f"{kind} does not accept {flag}")
        if params.choices[flag] not in allowed:
            raise ValueError(f"{flag} must be one of {', '.join(sorted(allowed))}")
        argv.append(f"{flag}={params.choices[flag]}")
    for flag in sorted(params.dates):
        if flag not in parameterized.dates:
            raise ValueError(f"{kind} does not accept {flag}")
        argv.append(f"{flag}={params.dates[flag].isoformat()}")
    for flag in sorted(params.fractions):
        if flag not in parameterized.fractions:
            raise ValueError(f"{kind} does not accept {flag}")
        argv.append(f"{flag}={float(params.fractions[flag])!r}")
    for flag in sorted(params.input_files):
        if flag not in parameterized.input_flags:
            raise ValueError(f"{kind} does not accept {flag}")
        argv.append(f"{flag}={params.input_files[flag]}")

    if params.input_path is not None:
        if parameterized.input_flag is None:
            raise ValueError(f"{kind} takes no input file")
        argv.append(f"{parameterized.input_flag}={params.input_path}")

    if params.destination is not None:
        if parameterized.destination_flag is None:
            raise ValueError(f"{kind} takes no destination")
        argv.append(f"{parameterized.destination_flag}={params.destination}")

    if parameterized.takes_archive:
        if params.archive is None:
            raise ValueError(f"{kind} acts on an archive and none was given")
        # After `--`, so the path is the positional argument whatever it
        # begins with.
        argv.extend(["--", str(params.archive)])
    elif params.archive is not None:
        raise ValueError(f"{kind} takes no archive")
    return argv


def _target(params: RunParams | None) -> str:
    """The lock key: the route's chosen target, else the job, else the kind."""
    if params is None:
        return ""
    if params.target is not None:
        return params.target
    return "" if params.job_id is None else str(params.job_id)


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


def hide_directory(data: dict[str, object], directory: HiddenDirectory) -> dict[str, object]:
    """Every string an event carries, with that directory's path removed."""
    hidden: dict[str, object] = {}
    for key, value in data.items():
        if isinstance(value, str):
            hidden[key] = directory.hide(value)
        elif isinstance(value, dict):
            hidden[key] = hide_directory(cast("dict[str, object]", value), directory)
        elif isinstance(value, list):
            hidden[key] = [
                directory.hide(item) if isinstance(item, str) else item
                for item in cast("list[object]", value)
            ]
        else:
            hidden[key] = value
    return hidden


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
    # Every file this process wrote for the run, removed when it ends.
    input_paths: tuple[Path, ...] = ()
    # What the run was asked to do, with its files by content, so a second
    # attempt joins only when it asks for the same thing (review of PR #208).
    signature: tuple[str, ...] = ()
    hidden: tuple[HiddenDirectory, ...] = ()


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
        # Inputs of runs the journal shows were cut off. Read here, removed
        # only when the served app starts (`release_interrupted_inputs`):
        # constructing a manager happens on every import of the app,
        # `just contract` included, and must delete nothing (review of
        # PR #208).
        self._interrupted_inputs: list[Path] = []
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
        """Start a run, return the active run it repeats, or refuse.

        The lock is per (kind, target) rather than per kind, so two jobs
        tailor at once while one job tailored twice joins the run already in
        flight. ADR-004 called for this under "artifact renders are per-slug
        locked"; spec 047 is where it was built.

        Joining is for the same request only: same options, files with the
        same bytes. A request with other options while one runs raises
        `RunConflictError` rather than being folded into a run that ignores
        them (review of PR #208).
        """
        # A kind that acts on everything locks on the empty target, which is
        # the one-at-a-time behaviour discovery always had. Kinds never
        # collide with each other because the lock is on the pair.
        target = _target(params)
        files = () if params is None else params.files
        try:
            command = (
                list(self._kind_commands[kind]) if params is None else build_command(kind, params)
            )
        except (KeyError, ValueError):
            # Refused before it became a run: its files go with the refusal.
            self._discard_input(params)
            raise
        signature = _signature(command, files)
        active = self.active_run(kind, target)
        if active is not None:
            # This attempt never becomes a run, so the files written for it
            # have no terminal state to be cleaned up by. Removing them here
            # is the difference between joining a run and leaking a file of
            # the operator's own words on every double click.
            self._discard_input(params)
            if active.signature != signature:
                raise RunConflictError(active)
            return active
        run = Run(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            command=command,
            target=target,
            input_paths=files,
            signature=signature,
            hidden=() if params is None else params.hidden,
        )
        self._runs[run.id] = run
        self._journal(run)
        self._tasks[run.id] = asyncio.create_task(self._execute(run))
        return run

    @staticmethod
    def _discard_input(params: RunParams | None) -> None:
        # Every file, not only the first: an attempt with three uploads that
        # joined a run left two behind when this removed `input_path` alone.
        for path in () if params is None else params.files:
            path.unlink(missing_ok=True)

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
        for path in run.input_paths:
            path.unlink(missing_ok=True)
        run.input_paths = ()

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
        scrubbed = scrub_event_data(data)
        for directory in run.hidden:
            scrubbed = hide_directory(scrubbed, directory)
        async with self._condition:
            run.events.append(RunEvent(id=len(run.events) + 1, type=event_type, data=scrubbed))
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
            # An archive's name for a verification, so its result outlives a
            # restart and the backups list can still mark it (spec 096). A
            # job id otherwise, or empty.
            "target": run.target,
            # The files the run holds, so a server that starts after this
            # one stopped mid-run knows which inputs were this run's. File
            # names this process chose, never their content (spec 095).
            "inputs": [str(path) for path in run.input_paths],
        }
        with self._journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def release_interrupted_inputs(self) -> list[Path]:
        """Remove the inputs of runs the journal shows were cut off.

        Called once by the served app as it starts, never at construction.
        Only files the journal names as an interrupted run's, and only inside
        the run inputs directory: a file no journaled run names may belong to
        a run another process has in flight, so it stays (review of PR #208).
        """
        removed: list[Path] = []
        for path in self._interrupted_inputs:
            if _in_inputs(path) and path.is_file():
                path.unlink(missing_ok=True)
                removed.append(path)
        self._interrupted_inputs = []
        return removed

    def _load_journal(self) -> None:
        """Rebuild terminal runs from the journal so restarts can list history."""
        if not self._journal_path.is_file():
            return
        held: dict[str, list[Path]] = {}
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
                held.pop(run_id, None)
            else:
                raw_inputs = record.get("inputs")
                names = cast("list[object]", raw_inputs) if isinstance(raw_inputs, list) else []
                held[run_id] = [Path(str(name)) for name in names if isinstance(name, str)]
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
                target=str(record.get("target") or ""),
            )
        self._interrupted_inputs = [path for paths in held.values() for path in paths]


def format_sse(event: RunEvent) -> str:
    payload = json.dumps({"type": event.type, **event.data}, sort_keys=True)
    return f"id: {event.id}\ndata: {payload}\n\n"
