"""The files that say a host process holds the tracker database (spec 075).

Spec 061's check runs before a host process opens the database, and cannot see
a container that starts afterwards. A lease closes that window from the other
side: the host writes one before it opens the file, and the container refuses
its own opens while any lease exists.

A lease is `<pid>.json` in `data/host-db-owners/` and holds exactly three
values: the pid, the process start time, and the subcommand name. Never an
argument value: arguments carry contact names and free text, and the lease is
echoed by the API, `/health` and `harrier doctor`.

**Written whole, created exclusively.** The content goes to a temporary file
first and is then hard-linked to its final name. `link` fails if the name
exists, which makes the create exclusive, and a reader never sees a file
half-written, so a lease being written is never mistaken for a broken one.

**Liveness is judged on the host only.** A lease is live when a process with
that pid exists and its start time matches the recorded one, so a pid reused
by an unrelated process is dead. The container cannot see host pids and
treats every lease file as live.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

LEASE_DIRNAME = "host-db-owners"
LEASE_SUFFIX = ".json"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Lease:
    pid: int
    started: str
    subcommand: str
    path: Path


def process_start_time(pid: int) -> str | None:
    """When `pid` started, as ISO 8601 UTC to the second, or None.

    Read with `ps`, which macOS and Linux both have and which reports the
    start time at one-second resolution. That resolution is the tolerance: a
    pid reused within the same second as its predecessor started would match.
    """
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = " ".join(result.stdout.split())
    if result.returncode != 0 or not text:
        return None
    try:
        started = datetime.strptime(text, "%a %b %d %H:%M:%S %Y").replace(tzinfo=UTC)
    except ValueError:
        return None
    return started.isoformat()


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def write_lease(directory: Path, pid: int, subcommand: str) -> Path:
    """Write this process's lease, replacing a stale file carrying its pid.

    A file named for this pid that this process did not write belongs to an
    earlier process that had the same pid, so it is stale by definition.
    """
    directory.mkdir(parents=True, exist_ok=True)
    started = process_start_time(pid) or UNKNOWN
    final = directory / f"{pid}{LEASE_SUFFIX}"
    body = json.dumps({"pid": pid, "started": started, "subcommand": subcommand})
    handle, scratch_name = tempfile.mkstemp(dir=directory, prefix=".lease-", suffix=".tmp")
    scratch = Path(scratch_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(body)
        try:
            os.link(scratch, final)
        except FileExistsError:
            final.unlink(missing_ok=True)
            os.link(scratch, final)
    finally:
        scratch.unlink(missing_ok=True)
    return final


def lease_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.suffix == LEASE_SUFFIX and not path.name.startswith(".")
    )


def read_lease(path: Path) -> Lease | None:
    """The lease in `path`, or None when it cannot be read as one."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Lease(
            pid=int(data["pid"]),
            started=str(data["started"]),
            subcommand=str(data["subcommand"]),
            path=path,
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def is_live(lease: Lease) -> bool:
    if not _pid_exists(lease.pid):
        return False
    return process_start_time(lease.pid) == lease.started


def remove_dead(directory: Path) -> list[Path]:
    """Remove leases whose process is gone or is a different process now.

    Run by every host invocation before it decides anything, so a lease left
    by a crashed host process is cleared by the next one. A file that cannot
    be read as a lease is left alone: it may be another tool's, and the
    container treating it as a hold is the safe failure.
    """
    removed: list[Path] = []
    for path in lease_files(directory):
        lease = read_lease(path)
        if lease is not None and not is_live(lease):
            path.unlink(missing_ok=True)
            removed.append(path)
    return removed


def oldest_hold(directory: Path, *, ignore_pid: int | None = None) -> tuple[str, str] | None:
    """The subcommand and start of the oldest lease, or None when there is none.

    A file present but unreadable is still a hold: the container cannot tell
    it apart from a lease being written, and refusing is the safe answer.
    `ignore_pid` leaves out the caller's own lease, which is not a hold
    against itself.
    """
    files = [
        path
        for path in lease_files(directory)
        if ignore_pid is None or path.name != f"{ignore_pid}{LEASE_SUFFIX}"
    ]
    if not files:
        return None
    leases = [lease for lease in (read_lease(path) for path in files) if lease is not None]
    if not leases:
        return (UNKNOWN, UNKNOWN)
    oldest = min(leases, key=lambda lease: lease.started)
    return (oldest.subcommand, oldest.started)
