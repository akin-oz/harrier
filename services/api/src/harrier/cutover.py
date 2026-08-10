"""The cutover: preflight, then an ordered sequence (spec 024).

Cutover plan phases 3 and 4. This is the one operation in the project that
is not reversible on a timescale that matters: it stops the system running
a real job search and starts a different one in its place. So the module is
built around two ideas.

**Preflight refuses rather than warns.** Every precondition the plan names
is checked, and the ones a machine cannot check are listed as attestations
the operator has to make explicitly. `run_cutover` will not execute unless
preflight passes, which makes "I forgot the parity checklist" impossible
rather than merely discouraged.

**Dry run is the default.** Every step reports what it would do and touches
nothing until `execute=True`. The effects that reach the machine (launchctl,
copying the old repo's data, installing plists) are injected, so the
sequence is testable without a real cutover ever happening.

Nothing here writes to the old repo. It is read for its data and its jobs
are unloaded; the directory itself stays exactly as it was, per the plan's
phase 0 rule that the old repo is read-only as a codebase.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from harrier.db import data_dir
from harrier.parity import checklist_status, parse_matrix
from harrier.parity.checklist import CHECKLIST_PATH
from harrier.schedule import default_launchctl

# The old system's launchd labels, from its committed plists.
OLD_LABELS = (
    "com.akinoztorun.jobsearch.discovery",
    "com.akinoztorun.jobsearch.daily-digest",
    "com.akinoztorun.jobsearch.gmail-watch",
)

# What phase 3 step 2 snapshots out of the old repo, relative to its root.
SNAPSHOT_TARGETS = ("tracker", "state", "gmail_handler.log", "incoming")

LaunchctlFn = Callable[[list[str]], tuple[int, str, str]]


class CutoverError(RuntimeError):
    pass


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str

    def line(self) -> str:
        return f"[{'ok' if self.passed else 'BLOCKED'}] {self.name}: {self.detail}"


@dataclass
class Preflight:
    checks: list[Check]
    attestations: list[str]

    @property
    def blocked(self) -> list[Check]:
        return [check for check in self.checks if not check.passed]

    @property
    def ready(self) -> bool:
        return not self.blocked

    def report(self) -> str:
        lines = [check.line() for check in self.checks]
        lines.append("")
        lines.append("Only you can confirm these; pass --attested once they are true:")
        lines.extend(f"  - {item}" for item in self.attestations)
        return "\n".join(lines)


def checklist_check(checklist_path: Path | None = None) -> Check:
    path = checklist_path if checklist_path is not None else CHECKLIST_PATH
    if not path.is_file():
        return Check("parity checklist", False, f"no checklist at {path}")
    status = checklist_status(path.read_text(encoding="utf-8"), parse_matrix())
    detail = f"{status.checked}/{status.total} decided, {status.waived} waived"
    if status.orphaned:
        detail += f", {len(status.orphaned)} retired items unresolved"
    return Check("parity checklist", status.complete, detail)


def old_jobs_check(labels: tuple[str, ...], launchctl: LaunchctlFn) -> Check:
    """There has to be something to quiesce. If the old jobs are already
    gone, either cutover ran or the old system stopped some other way, and
    either way this is not the clean starting state the plan assumes."""
    loaded = [label for label in labels if launchctl(["list", label])[0] == 0]
    if not loaded:
        return Check("old jobs loaded", False, "none of the old plists are loaded")
    return Check("old jobs loaded", True, f"{len(loaded)} of {len(labels)} loaded")


def tracker_check(conn: sqlite3.Connection) -> Check:
    """Migrated data has to be present, or go-live points the schedule at an
    empty tracker and the first run rediscovers a year of postings."""
    jobs = int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
    if jobs == 0:
        return Check("tracker migrated", False, "no jobs in the database")
    return Check("tracker migrated", True, f"{jobs} jobs")


def env_check(env_path: Path) -> Check:
    """The old repo's .env broke its own shell wrapper by wrapping a value
    onto a second line. harrier's loader skips such a line rather than
    dying, but the fallback window depends on the old system working, so a
    malformed line still blocks."""
    if not env_path.is_file():
        return Check("old .env parses", True, "no .env to check")
    bad: list[int] = []
    for number, raw in enumerate(
        env_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            bad.append(number)
    if bad:
        return Check(
            "old .env parses",
            False,
            f"line(s) {', '.join(str(n) for n in bad)} have no '=', so `set -a; . .env` "
            "will try to execute them and the old wrappers will exit 127",
        )
    return Check("old .env parses", True, "every line is an assignment")


def preflight(
    conn: sqlite3.Connection,
    *,
    old_root: Path,
    checklist_path: Path | None = None,
    labels: tuple[str, ...] = OLD_LABELS,
    launchctl: LaunchctlFn | None = None,
) -> Preflight:
    runner = launchctl if launchctl is not None else default_launchctl
    return Preflight(
        checks=[
            checklist_check(checklist_path),
            old_jobs_check(labels, runner),
            tracker_check(conn),
            env_check(old_root / ".env"),
        ],
        attestations=[
            "the dual-run period ran for at least a full week, including a "
            "weekday morning so the Apify path and its cost gate were exercised",
            "at least one `harrier parity diff` over a post-migration shadow run was clean",
            "you can watch the next scheduled run land today",
        ],
    )


@dataclass
class CutoverResult:
    executed: bool
    lines: list[str] = field(default_factory=list[str])
    failures: list[str] = field(default_factory=list[str])
    snapshot: Path | None = None

    @property
    def ok(self) -> bool:
        return not self.failures


def snapshot_dir(stamp: str) -> Path:
    """Outside the repo, like every other backup (ADR-008)."""
    return Path.home() / "Backups" / "harrier-cutover" / stamp


def quiesce(
    labels: tuple[str, ...], launchctl: LaunchctlFn, *, execute: bool, result: CutoverResult
) -> None:
    for label in labels:
        if not execute:
            result.lines.append(f"would unload {label}")
            continue
        code, _out, err = launchctl(["bootout", f"gui/{_uid()}/{label}"])
        if code == 0:
            result.lines.append(f"unloaded {label}")
        elif "No such process" in err or code == 3:
            result.lines.append(f"{label} was not loaded")
        else:
            # A job still running while its replacement starts would mean two
            # systems writing application state, which the plan forbids.
            result.failures.append(f"{label}: unload failed: {err.strip() or code}")


def _uid() -> int:
    import os

    return os.getuid()


def snapshot(old_root: Path, destination: Path, *, execute: bool, result: CutoverResult) -> None:
    present = [name for name in SNAPSHOT_TARGETS if (old_root / name).exists()]
    if not present:
        result.failures.append(f"nothing to snapshot under {old_root}")
        return
    if not execute:
        result.lines.append(f"would snapshot {', '.join(present)} to {destination}")
        return
    destination.mkdir(parents=True, exist_ok=True)
    for name in present:
        source = old_root / name
        target = destination / name
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)
    result.snapshot = destination
    result.lines.append(f"snapshotted {', '.join(present)} to {destination}")


def verify(conn: sqlite3.Connection, *, result: CutoverResult) -> None:
    jobs = int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
    contacts = int(conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0])
    documents = int(conn.execute("SELECT COUNT(*) FROM profile_documents").fetchone()[0])
    result.lines.append(f"verify: {jobs} jobs, {contacts} contacts, {documents} profile documents")
    if jobs == 0:
        result.failures.append("verify: the tracker is empty after migration")


def write_log(stamp: str, result: CutoverResult, *, execute: bool) -> Path:
    """The cutover record. It goes to data/, never docs/.

    docs/cutover-plan.md phase 4 says docs/cutover-log.md, and that is
    wrong for the same reason spec 020's first draft was: a dated record of
    a real person's job search, with row counts, is operational state about
    them (ADR-008). The plan is amended by this spec rather than followed.
    """
    path = data_dir() / "cutover" / f"{stamp}.md"
    if not execute:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join([f"# Cutover {stamp}", "", *(f"- {line}" for line in result.lines), ""])
    path.write_text(body, encoding="utf-8")
    return path


def run_cutover(
    conn: sqlite3.Connection,
    *,
    old_root: Path,
    stamp: str,
    execute: bool = False,
    attested: bool = False,
    labels: tuple[str, ...] = OLD_LABELS,
    launchctl: LaunchctlFn | None = None,
    install: Callable[[], list[str]] | None = None,
    checklist_path: Path | None = None,
) -> CutoverResult:
    """Phases 3 and 4, in order. Refuses to execute unless preflight passes
    and the operator has attested to what a machine cannot check."""
    runner = launchctl if launchctl is not None else default_launchctl
    result = CutoverResult(executed=execute)

    checks = preflight(
        conn,
        old_root=old_root,
        checklist_path=checklist_path,
        labels=labels,
        launchctl=runner,
    )
    if execute and not checks.ready:
        raise CutoverError(
            "preflight is blocked; run `harrier cutover preflight` and clear it first:\n"
            + "\n".join(check.line() for check in checks.blocked)
        )
    if execute and not attested:
        raise CutoverError(
            "the attestations have not been made; re-run with --attested once every "
            "line under `harrier cutover preflight` is true"
        )

    quiesce(labels, runner, execute=execute, result=result)
    if result.failures:
        # Stop before touching data: a half-quiesced system with a snapshot
        # taken mid-write is worse than one that never started.
        result.lines.append("stopped after quiesce because a job would not unload")
        return result

    snapshot(old_root, snapshot_dir(stamp), execute=execute, result=result)
    if result.failures:
        return result

    verify(conn, result=result)
    if result.failures:
        return result

    if install is not None:
        result.lines.extend(install() if execute else ["would install the harrier schedule"])
    log_path = write_log(stamp, result, execute=execute)
    result.lines.append(f"{'wrote' if execute else 'would write'} the cutover log to {log_path}")
    return result


def utc_stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%d-%H%M")
