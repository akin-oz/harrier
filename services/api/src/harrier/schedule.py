"""Generated launchd plists (spec 020, ADR-006).

Plists are rendered from config/schedule.json with the repo path, user,
and interpreter resolved at install time, and they invoke the harrier
CLI directly. Two defect classes die here: a committed plist can no
longer carry a stale absolute path, and there is no shell wrapper
sourcing .env, so a malformed .env line cannot abort a scheduled run.
"""

from __future__ import annotations

import json
import os
import plistlib
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any, cast

from harrier.db import data_dir
from harrier.paths import repo_root

SCHEDULE_CONFIG_PATH = Path("config") / "schedule.json"
DEFAULT_LABEL_PREFIX = "dev.harrier"

# Injected so tests never shell out; returns (returncode, stdout, stderr).
Launchctl = Callable[[list[str]], tuple[int, str, str]]


class ScheduleConfigError(ValueError):
    pass


def _validate_identifier(value: str, field: str, context: str) -> str:
    """Names and label prefixes become plist and log path components, so a
    separator, dot segment, or absolute value could escape the target
    directory (review finding)."""
    text = value.strip()
    if not text:
        raise ScheduleConfigError(f"{context}: missing {field}")
    if text.startswith("/") or "\\" in text:
        raise ScheduleConfigError(f"{context}: {field} must not be an absolute path: {text!r}")
    if "/" in text or os.sep in text:
        raise ScheduleConfigError(f"{context}: {field} must not contain a path separator: {text!r}")
    if text in {".", ".."} or any(part in {".", ".."} for part in text.split(".")):
        raise ScheduleConfigError(f"{context}: {field} must not contain a dot segment: {text!r}")
    return text


def _require_int(value: object, field: str, context: str, *, low: int, high: int) -> int:
    """A JSON boolean satisfies isinstance(x, int) and would render as a
    boolean plist value, so the type is checked exactly (review finding)."""
    if type(value) is not int or not low <= value <= high:
        raise ScheduleConfigError(f"{context}: {field} must be {low}-{high}, got {value!r}")
    return value


@dataclass(frozen=True)
class CalendarTime:
    hour: int
    minute: int
    # 1 (Monday) to 7 (Sunday), launchd's own numbering; None runs every day
    # (spec 097). A weekly job is the academic discovery cadence.
    weekday: int | None = None


@dataclass(frozen=True)
class ScheduleJob:
    name: str
    command: tuple[str, ...]
    kind: str
    times: tuple[CalendarTime, ...] = ()
    seconds: int = 0

    def label(self, prefix: str) -> str:
        return f"{prefix}.{self.name}"


@dataclass
class JobStatus:
    name: str
    label: str
    installed: bool = False
    loaded: bool = False
    drifted: bool = False
    last_exit_status: str = ""
    next_run: str = ""

    def line(self) -> str:
        state = "installed" if self.installed else "missing"
        loaded = "loaded" if self.loaded else "not loaded"
        drift = " DRIFTED" if self.drifted else ""
        exit_text = f" last_exit={self.last_exit_status}" if self.last_exit_status else ""
        next_text = f" next={self.next_run}" if self.next_run else ""
        problem = " PROBLEM" if self.problem else ""
        return f"{self.name}: {state}, {loaded}{drift}{exit_text}{next_text}{problem}"

    @property
    def problem(self) -> str:
        """Why this job is not healthy, or "" when it is.

        The status command printed its table and returned zero regardless of
        what the table said, so a missing, unloaded, drifted or failing job
        was reported in prose nobody had to read and in an exit code that
        never changed (spec 040).
        """
        if not self.installed:
            return "not installed"
        if not self.loaded:
            return "installed but not loaded"
        if self.drifted:
            return "drifted from the generated definition"
        if self.last_exit_status and self.last_exit_status not in ("0", ""):
            return f"last run exited {self.last_exit_status}"
        return ""


@dataclass
class InstallResult:
    written: list[Path] = field(default_factory=list[Path])
    loaded: list[str] = field(default_factory=list[str])
    failures: list[str] = field(default_factory=list[str])
    lines: list[str] = field(default_factory=list[str])

    @property
    def ok(self) -> bool:
        return not self.failures


@dataclass
class UninstallResult:
    removed: list[Path] = field(default_factory=list[Path])
    failures: list[str] = field(default_factory=list[str])
    lines: list[str] = field(default_factory=list[str])

    @property
    def ok(self) -> bool:
        return not self.failures


# launchctl bootout on a job that is not loaded; tolerated everywhere.
_NOT_LOADED_CODES = {3, 113}


def _is_not_loaded(code: int, stderr: str) -> bool:
    return code in _NOT_LOADED_CODES or "no such process" in stderr.lower()


# launchctl exists only on macOS. `subprocess.run` raises FileNotFoundError
# rather than returning a code, so every caller that expected a code got an
# exception instead, and the README's "the scheduler is not portable, and
# reports as much on other systems" was false: it did not report, it crashed.
# Found when a cutover preflight test first ran on Linux CI (spec 045).
LAUNCHCTL_ABSENT = 127


def default_launchctl(args: list[str]) -> tuple[int, str, str]:
    try:
        result = subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return (
            LAUNCHCTL_ABSENT,
            "",
            "launchctl not found: scheduling is launchd and needs macOS",
        )
    return result.returncode, result.stdout, result.stderr


def launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def logs_dir() -> Path:
    return data_dir() / "logs"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _parse_job(raw: object, index: int) -> ScheduleJob:
    context = f"jobs[{index}]"
    if not isinstance(raw, dict):
        raise ScheduleConfigError(f"{context}: not an object")
    job = cast("dict[str, Any]", raw)
    name = _validate_identifier(str(job.get("name") or ""), "name", context)
    command_raw = job.get("command")
    if not isinstance(command_raw, list) or not command_raw:
        raise ScheduleConfigError(f"{context} ({name}): missing command")
    command = tuple(str(item) for item in cast("list[object]", command_raw))
    trigger_raw = job.get("trigger")
    if not isinstance(trigger_raw, dict):
        raise ScheduleConfigError(f"{context} ({name}): missing trigger")
    trigger = cast("dict[str, Any]", trigger_raw)
    kind = str(trigger.get("kind") or "").strip()
    if kind == "calendar":
        times_raw = trigger.get("times")
        if not isinstance(times_raw, list) or not times_raw:
            raise ScheduleConfigError(f"{context} ({name}): calendar trigger needs times")
        times: list[CalendarTime] = []
        for entry in cast("list[object]", times_raw):
            if not isinstance(entry, dict):
                raise ScheduleConfigError(f"{context} ({name}): a time is not an object")
            time_entry = cast("dict[str, Any]", entry)
            hour = _require_int(
                time_entry.get("hour"), "hour", f"{context} ({name})", low=0, high=23
            )
            minute = _require_int(
                time_entry.get("minute", 0), "minute", f"{context} ({name})", low=0, high=59
            )
            weekday: int | None = None
            if "weekday" in time_entry:
                weekday = _require_int(
                    time_entry.get("weekday"), "weekday", f"{context} ({name})", low=1, high=7
                )
            times.append(CalendarTime(hour=hour, minute=minute, weekday=weekday))
        return ScheduleJob(name=name, command=command, kind=kind, times=tuple(times))
    if kind == "interval":
        seconds = _require_int(
            trigger.get("seconds"), "seconds", f"{context} ({name})", low=1, high=86400
        )
        return ScheduleJob(name=name, command=command, kind=kind, seconds=seconds)
    raise ScheduleConfigError(f"{context} ({name}): unknown trigger kind {kind!r}")


def load_schedule(path: Path | None = None) -> tuple[str, list[ScheduleJob]]:
    config_path = path if path is not None else SCHEDULE_CONFIG_PATH
    try:
        parsed: object = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ScheduleConfigError(f"cannot read {config_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ScheduleConfigError(f"{config_path} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ScheduleConfigError(f"{config_path} is not an object")
    config = cast("dict[str, Any]", parsed)
    prefix = _validate_identifier(
        str(config.get("label_prefix") or DEFAULT_LABEL_PREFIX), "label_prefix", str(config_path)
    )
    jobs_raw = config.get("jobs")
    if not isinstance(jobs_raw, list) or not jobs_raw:
        raise ScheduleConfigError(f"{config_path}: no jobs")
    jobs = [_parse_job(item, index) for index, item in enumerate(cast("list[object]", jobs_raw))]
    # A duplicate name would overwrite one plist and make status report two
    # jobs for one loaded service (review finding).
    seen: set[str] = set()
    for job in jobs:
        if job.name in seen:
            raise ScheduleConfigError(f"{config_path}: duplicate job name {job.name!r}")
        seen.add(job.name)
    return prefix, jobs


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_plist_dict(
    job: ScheduleJob,
    prefix: str,
    *,
    root: Path,
    interpreter: Path,
    log_directory: Path,
) -> dict[str, Any]:
    """The plist body. ProgramArguments invokes the CLI module directly:
    no shell wrapper, so .env parsing stays in Python (spec 020)."""
    label = job.label(prefix)
    body: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": [
            str(interpreter),
            "-m",
            "harrier_cli.main",
            *job.command,
        ],
        "WorkingDirectory": str(root),
        "StandardOutPath": str(log_directory / f"{job.name}.stdout.log"),
        "StandardErrorPath": str(log_directory / f"{job.name}.stderr.log"),
        "RunAtLoad": False,
    }
    if job.kind == "calendar":
        intervals: list[dict[str, int]] = []
        for time in job.times:
            interval = {"Hour": time.hour, "Minute": time.minute}
            if time.weekday is not None:
                interval["Weekday"] = time.weekday
            intervals.append(interval)
        body["StartCalendarInterval"] = intervals if len(intervals) > 1 else intervals[0]
    else:
        body["StartInterval"] = job.seconds
    return body


def render_plist_bytes(
    job: ScheduleJob,
    prefix: str,
    *,
    root: Path,
    interpreter: Path,
    log_directory: Path,
) -> bytes:
    return plistlib.dumps(
        render_plist_dict(
            job, prefix, root=root, interpreter=interpreter, log_directory=log_directory
        )
    )


def next_run_after(job: ScheduleJob, now: datetime) -> datetime:
    if job.kind == "interval":
        return now + timedelta(seconds=job.seconds)
    candidates: list[datetime] = []
    for time in job.times:
        today = now.replace(hour=time.hour, minute=time.minute, second=0, microsecond=0)
        if time.weekday is None:
            candidates.append(today if today > now else today + timedelta(days=1))
            continue
        # The first day on or after today with the job's weekday whose time
        # is still ahead; a full week on when today's time has passed.
        ahead = (time.weekday - today.isoweekday()) % 7
        candidate = today + timedelta(days=ahead)
        candidates.append(candidate if candidate > now else candidate + timedelta(days=7))
    return min(candidates)


# ---------------------------------------------------------------------------
# Install, status, uninstall
# ---------------------------------------------------------------------------


def _plist_path(agents_dir: Path, label: str) -> Path:
    return agents_dir / f"{label}.plist"


def install_schedule(
    *,
    config_path: Path | None = None,
    agents_dir: Path | None = None,
    root: Path | None = None,
    interpreter: Path | None = None,
    log_directory: Path | None = None,
    dry_run: bool = False,
    launchctl: Launchctl = default_launchctl,
) -> InstallResult:
    prefix, jobs = load_schedule(config_path)
    agents = agents_dir if agents_dir is not None else launch_agents_dir()
    target_root = root if root is not None else repo_root()
    python_path = interpreter if interpreter is not None else Path(sys.executable)
    logs = log_directory if log_directory is not None else logs_dir()
    result = InstallResult()

    for job in jobs:
        label = job.label(prefix)
        path = _plist_path(agents, label)
        body = render_plist_bytes(
            job, prefix, root=target_root, interpreter=python_path, log_directory=logs
        )
        if dry_run:
            result.lines.append(f"[dry-run] would write {path} and load {label}")
            continue
        agents.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        result.written.append(path)
        # bootout first so a reinstall replaces the loaded definition; a
        # not-loaded job makes bootout fail harmlessly.
        launchctl(["bootout", f"gui/{_uid()}/{label}"])
        code, _stdout, stderr = launchctl(["bootstrap", f"gui/{_uid()}", str(path)])
        if code != 0:
            detail = stderr.strip() or f"exit {code}"
            # A failed load must reach the caller's exit status, not just
            # the printed lines (review finding).
            result.failures.append(f"{label}: load failed: {detail}")
            result.lines.append(f"load failed for {label}: {detail}")
        else:
            result.loaded.append(label)
            result.lines.append(f"installed and loaded {label}")
    return result


def _uid() -> int:
    import os

    return os.getuid()


def _parse_launchctl_print(output: str) -> tuple[bool, str]:
    """(loaded, last exit status) from a launchctl print block."""
    if not output.strip():
        return False, ""
    last_exit = ""
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("last exit code =") or stripped.startswith("last exit status ="):
            last_exit = stripped.split("=", 1)[1].strip()
    return True, last_exit


def schedule_status(
    *,
    config_path: Path | None = None,
    agents_dir: Path | None = None,
    root: Path | None = None,
    interpreter: Path | None = None,
    log_directory: Path | None = None,
    now: datetime | None = None,
    launchctl: Launchctl = default_launchctl,
) -> list[JobStatus]:
    prefix, jobs = load_schedule(config_path)
    agents = agents_dir if agents_dir is not None else launch_agents_dir()
    target_root = root if root is not None else repo_root()
    python_path = interpreter if interpreter is not None else Path(sys.executable)
    logs = log_directory if log_directory is not None else logs_dir()
    reference = now if now is not None else datetime.now()

    statuses: list[JobStatus] = []
    for job in jobs:
        label = job.label(prefix)
        status = JobStatus(name=job.name, label=label)
        path = _plist_path(agents, label)
        expected = render_plist_bytes(
            job, prefix, root=target_root, interpreter=python_path, log_directory=logs
        )
        if path.exists():
            status.installed = True
            # Drift is the defect that let the old repo's committed plists
            # point at a path that no longer existed (spec 020).
            status.drifted = path.read_bytes() != expected
        code, stdout, _stderr = launchctl(["print", f"gui/{_uid()}/{label}"])
        if code == 0:
            status.loaded, status.last_exit_status = _parse_launchctl_print(stdout)
        status.next_run = next_run_after(job, reference).isoformat(timespec="minutes")
        statuses.append(status)
    return statuses


# ---------------------------------------------------------------------------
# What a process without launchctl can say (spec 050 as amended by spec 096)
# ---------------------------------------------------------------------------

# A job is overdue when its last success is older than this many of its
# longest gaps between scheduled runs. One missed run is a laptop asleep at
# the scheduled minute; two in a row is the job that stopped (spec 050).
OVERDUE_GAPS = 2

_WEEKDAYS = ("Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays")


@dataclass(frozen=True)
class SuccessRecord:
    """One last-success record a scheduled job writes (spec 029)."""

    key: str
    last_success_at: str | None
    summary: str
    overdue: bool


@dataclass(frozen=True)
class JobHealth:
    """A scheduled job as the records describe it. Installed and loaded are
    not here: only launchctl on the host knows them."""

    name: str
    cadence: str
    records: tuple[SuccessRecord, ...]


def describe_cadence(job: ScheduleJob) -> str:
    """The cadence in words, from the definition rather than from launchd."""
    if job.kind == "interval":
        for unit, seconds in (("hour", 3600), ("minute", 60), ("second", 1)):
            if job.seconds % seconds == 0:
                count = job.seconds // seconds
                return f"every {count} {unit}{'s' if count != 1 else ''}"
    groups: dict[int | None, list[str]] = {}
    for time in job.times:
        groups.setdefault(time.weekday, []).append(f"{time.hour:02d}:{time.minute:02d}")
    parts: list[str] = []
    for weekday, clock in sorted(groups.items(), key=lambda item: item[0] or 0):
        when = "daily" if weekday is None else _WEEKDAYS[weekday - 1]
        parts.append(f"{when} at {', '.join(sorted(clock))}")
    return "; ".join(parts)


def longest_gap(job: ScheduleJob) -> timedelta:
    """The longest time between two of this job's scheduled runs.

    Walked with `next_run_after` over two weeks, so a weekly job and a job
    with uneven times of day are measured by the same rule launchd follows.
    """
    if job.kind == "interval":
        return timedelta(seconds=job.seconds)
    start = datetime(2024, 1, 1)  # a Monday, so every weekday is crossed
    runs: list[datetime] = []
    moment = start
    while moment < start + timedelta(days=15):
        moment = next_run_after(job, moment)
        runs.append(moment)
    return max(later - earlier for earlier, later in pairwise(runs))


def success_keys(conn: sqlite3.Connection, job: ScheduleJob) -> list[str]:
    """The last-success records this job writes, read from its command.

    `discover --configured-tracks` writes one per configured academic track.
    A command that records no success gets no keys, and is reported as such
    rather than as healthy.
    """
    from harrier.runoutcome import DIGEST_JOB, DISCOVERY_JOB, MAIL_WATCH_JOB

    verb = job.command[0] if job.command else ""
    if verb == "discover" and "--configured-tracks" in job.command:
        from harrier.digest import academic_discovery_jobs

        return academic_discovery_jobs(conn)
    return {
        "discover": [DISCOVERY_JOB],
        "digest": [DIGEST_JOB],
        "gmail-watch": [MAIL_WATCH_JOB],
    }.get(verb, [])


def _moment(timestamp: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def job_health(
    conn: sqlite3.Connection, *, config_path: Path | None = None, now: datetime | None = None
) -> list[JobHealth]:
    """Each scheduled job's cadence and last successes, and which are overdue.

    Raises `ScheduleConfigError` when the definition cannot be read, so the
    caller reports the loader's words rather than an empty, healthy list.
    """
    from harrier.runoutcome import all_last_success, describe_age

    _prefix, jobs = load_schedule(config_path)
    recorded = all_last_success(conn)
    reference = now if now is not None else datetime.now(UTC)
    health: list[JobHealth] = []
    for job in jobs:
        limit = longest_gap(job) * OVERDUE_GAPS
        records: list[SuccessRecord] = []
        for key in success_keys(conn, job):
            stamp = recorded.get(key)
            moment = None if stamp is None else _moment(stamp)
            records.append(
                SuccessRecord(
                    key=key,
                    last_success_at=stamp,
                    summary=describe_age(key, stamp, now=reference),
                    overdue=moment is None or reference - moment > limit,
                )
            )
        health.append(JobHealth(job.name, describe_cadence(job), tuple(records)))
    return health


def uninstall_schedule(
    *,
    config_path: Path | None = None,
    agents_dir: Path | None = None,
    launchctl: Launchctl = default_launchctl,
) -> UninstallResult:
    prefix, jobs = load_schedule(config_path)
    agents = agents_dir if agents_dir is not None else launch_agents_dir()
    result = UninstallResult()
    for job in jobs:
        label = job.label(prefix)
        code, _stdout, stderr = launchctl(["bootout", f"gui/{_uid()}/{label}"])
        unloaded = code == 0 or _is_not_loaded(code, stderr)
        if not unloaded:
            # Removing the plist while the job stays loaded would leave it
            # running with no on-disk definition (review finding).
            detail = stderr.strip() or f"exit {code}"
            result.failures.append(f"{label}: unload failed: {detail}")
            result.lines.append(f"unload failed for {label}: {detail}; plist kept")
            continue
        path = _plist_path(agents, label)
        if path.exists():
            path.unlink()
            result.removed.append(path)
            result.lines.append(f"removed {path}")
        else:
            result.lines.append(f"not installed: {label}")
    return result
