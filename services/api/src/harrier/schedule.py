"""Generated launchd plists (spec 020, ADR-006).

Plists are rendered from config/schedule.json with the repo path, user,
and interpreter resolved at install time, and they invoke the harrier
CLI directly. Two defect classes die here: a committed plist can no
longer carry a stale absolute path, and there is no shell wrapper
sourcing .env, so a malformed .env line cannot abort a scheduled run.
"""

from __future__ import annotations

import json
import plistlib
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from harrier.db import data_dir

SCHEDULE_CONFIG_PATH = Path("config") / "schedule.json"
DEFAULT_LABEL_PREFIX = "dev.harrier"

# Injected so tests never shell out; returns (returncode, stdout, stderr).
Launchctl = Callable[[list[str]], tuple[int, str, str]]


class ScheduleConfigError(ValueError):
    pass


@dataclass(frozen=True)
class CalendarTime:
    hour: int
    minute: int


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
        return f"{self.name}: {state}, {loaded}{drift}{exit_text}{next_text}"


@dataclass
class InstallResult:
    written: list[Path] = field(default_factory=list[Path])
    loaded: list[str] = field(default_factory=list[str])
    lines: list[str] = field(default_factory=list[str])


def default_launchctl(args: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)
    return result.returncode, result.stdout, result.stderr


def launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def repo_root() -> Path:
    """The repo root resolved from this module, never hardcoded."""
    return Path(__file__).resolve().parents[4]


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
    name = str(job.get("name") or "").strip()
    if not name:
        raise ScheduleConfigError(f"{context}: missing name")
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
            hour = time_entry.get("hour")
            minute = time_entry.get("minute", 0)
            if not isinstance(hour, int) or not 0 <= hour <= 23:
                raise ScheduleConfigError(f"{context} ({name}): hour must be 0-23, got {hour!r}")
            if not isinstance(minute, int) or not 0 <= minute <= 59:
                raise ScheduleConfigError(
                    f"{context} ({name}): minute must be 0-59, got {minute!r}"
                )
            times.append(CalendarTime(hour=hour, minute=minute))
        return ScheduleJob(name=name, command=command, kind=kind, times=tuple(times))
    if kind == "interval":
        seconds = trigger.get("seconds")
        if not isinstance(seconds, int) or seconds <= 0:
            raise ScheduleConfigError(
                f"{context} ({name}): interval seconds must be a positive integer, got {seconds!r}"
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
    prefix = str(config.get("label_prefix") or DEFAULT_LABEL_PREFIX)
    jobs_raw = config.get("jobs")
    if not isinstance(jobs_raw, list) or not jobs_raw:
        raise ScheduleConfigError(f"{config_path}: no jobs")
    jobs = [_parse_job(item, index) for index, item in enumerate(cast("list[object]", jobs_raw))]
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
        intervals = [{"Hour": time.hour, "Minute": time.minute} for time in job.times]
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
        candidates.append(today if today > now else today + timedelta(days=1))
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
            result.lines.append(f"load failed for {label}: {stderr.strip()}")
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


def uninstall_schedule(
    *,
    config_path: Path | None = None,
    agents_dir: Path | None = None,
    launchctl: Launchctl = default_launchctl,
) -> list[str]:
    prefix, jobs = load_schedule(config_path)
    agents = agents_dir if agents_dir is not None else launch_agents_dir()
    lines: list[str] = []
    for job in jobs:
        label = job.label(prefix)
        launchctl(["bootout", f"gui/{_uid()}/{label}"])
        path = _plist_path(agents, label)
        if path.exists():
            path.unlink()
            lines.append(f"removed {path}")
        else:
            lines.append(f"not installed: {label}")
    return lines
