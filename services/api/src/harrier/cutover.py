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

import json
import os
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import cast

from harrier.db import data_dir
from harrier.parity import checklist_status, parse_matrix
from harrier.parity.checklist import CHECKLIST_PATH
from harrier.schedule import default_launchctl, launch_agents_dir

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


# The steps after quiesce, in order. Named so the record of what completed
# is a list of these rather than prose, which is what makes resuming a
# second invocation possible rather than a guess (spec 037).
STEP_SNAPSHOT = "snapshot"
STEP_VERIFY = "verify"
STEP_INSTALL = "install"
STEPS = (STEP_SNAPSHOT, STEP_VERIFY, STEP_INSTALL)


@dataclass
class CutoverResult:
    executed: bool
    lines: list[str] = field(default_factory=list[str])
    failures: list[str] = field(default_factory=list[str])
    blocked: list[str] = field(default_factory=list[str])
    snapshot: Path | None = None
    # What quiesce stopped. Held on the result rather than locally, because a
    # failure three steps later still has to put these back and the old code
    # had already forgotten them (spec 037).
    unloaded: list[str] = field(default_factory=list[str])
    completed: list[str] = field(default_factory=list[str])

    @property
    def ok(self) -> bool:
        return not self.failures and not self.blocked


def snapshot_dir(stamp: str) -> Path:
    """Outside the repo, like every other backup (ADR-008)."""
    return Path.home() / "Backups" / "harrier-cutover" / stamp


def quiesce(
    labels: tuple[str, ...],
    launchctl: LaunchctlFn,
    *,
    execute: bool,
    result: CutoverResult,
    agents_dir: Path | None = None,
) -> None:
    """Stop every old job, or leave the old system exactly as it was.

    A failed unload cannot be shrugged off and it cannot be pressed through:
    either would end with some old jobs stopped and one still running, which
    is two systems able to write application state, and the plan forbids
    that outright. So the first failure aborts and rolls back, reloading
    what was already unloaded (review finding on PR #22, which pointed out
    that the loop contradicted this comment by continuing).
    """
    directory = agents_dir if agents_dir is not None else launch_agents_dir()
    unloaded = result.unloaded
    for label in labels:
        if not execute:
            result.lines.append(f"would unload {label}")
            continue
        code, _out, err = launchctl(["bootout", f"gui/{_uid()}/{label}"])
        if code == 0:
            unloaded.append(label)
            result.lines.append(f"unloaded {label}")
        elif "No such process" in err or code == 3:
            result.lines.append(f"{label} was not loaded")
        else:
            result.failures.append(f"{label}: unload failed: {err.strip() or code}")
            _rollback(unloaded, launchctl, directory, result)
            return


def _rollback(
    unloaded: list[str], launchctl: LaunchctlFn, agents_dir: Path, result: CutoverResult
) -> None:
    """Put back what was already stopped, so the old system is running again.

    Reported line by line including its own failures: a rollback that itself
    fails leaves jobs down, and the operator has to know which ones to start
    by hand rather than discovering it at the next scheduled run.
    """
    if not unloaded:
        result.lines.append("nothing to roll back")
        return
    for label in reversed(unloaded):
        plist = agents_dir / f"{label}.plist"
        if not plist.is_file():
            # The old arrangement is the one the README describes as the
            # defect being fixed, so assuming the standard directory is
            # exactly the assumption most likely to be wrong here. Say what
            # the operator has to do by hand rather than reporting a
            # rollback that did not happen (spec 037).
            result.failures.append(
                f"{label}: cannot roll back, no plist at {plist}. "
                f"Start it by hand: launchctl bootstrap gui/{_uid()} <path to {label}.plist>"
            )
            continue
        code, _out, err = launchctl(["bootstrap", f"gui/{_uid()}", str(plist)])
        if code == 0:
            result.lines.append(f"rolled back: reloaded {label}")
        else:
            result.failures.append(
                f"{label}: rollback failed, it is still unloaded: {err.strip() or code}"
            )


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


class _StepFailed(RuntimeError):
    """A step reported a failure through the result rather than raising."""

    def __init__(self, step: str) -> None:
        self.step = step
        super().__init__(step)


def progress_path(stamp: str) -> Path:
    """Beside the log, so a resumed run reads what the failed one wrote."""
    return data_dir() / "cutover" / f"{stamp}.progress.json"


def resume_from(stamp: str) -> tuple[str, ...]:
    """The steps a previous invocation completed, if any.

    Cutover is the one operation here that is neither idempotent nor
    repeatable, so a partial failure has to be continuable. Written after
    each step rather than at the end, because the failure this protects
    against is the process not reaching the end.
    """
    path = progress_path(stamp)
    if not path.is_file():
        return ()
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        # Not "no record". A record that exists and cannot be read means a
        # previous invocation got far enough to write one, and cutover is
        # neither idempotent nor repeatable, so restarting from zero would
        # take a second snapshot over a system that is already half moved.
        # Refusing and naming the file is the only safe answer (review
        # finding on PR #37).
        raise CutoverError(
            f"the progress record at {path} exists but cannot be read ({error}). "
            "A previous cutover wrote it, so this is not a fresh start. "
            "Inspect it, and delete it only if you are certain no step ran."
        ) from error
    if not isinstance(parsed, dict):
        raise CutoverError(
            f"the progress record at {path} is not a cutover record. "
            "Inspect it, and delete it only if you are certain no step ran."
        )
    steps = cast("dict[str, object]", parsed).get("completed")
    if not isinstance(steps, list):
        return ()
    return tuple(str(step) for step in cast("list[object]", steps) if str(step) in STEPS)


def _record_progress(stamp: str, result: CutoverResult, *, execute: bool) -> None:
    if not execute:
        return
    path = progress_path(stamp)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written through a replacement rather than in place. `write_text`
    # truncates first, so an interruption between the truncate and the write
    # left an empty file, and an empty file used to read as "nothing has
    # happened yet" (review finding on PR #37).
    scratch = path.with_name(f"{path.name}.partial")
    scratch.write_text(json.dumps({"completed": result.completed}, indent=2), encoding="utf-8")
    os.replace(scratch, path)


def _finish(stamp: str, result: CutoverResult, *, execute: bool) -> None:
    """Write the log on every path out, including the failing ones."""
    log_path = write_log(stamp, result, execute=execute)
    result.lines.append(f"{'wrote' if execute else 'would write'} the cutover log to {log_path}")
    if execute and result.ok:
        progress_path(stamp).unlink(missing_ok=True)


def _refuse(stamp: str, result: CutoverResult, *, execute: bool, reason: str) -> None:
    """Log the refusal, then raise it.

    Spec 037 requires a record for refusals, and these two paths raised
    before reaching `_finish`, so the executions that never started were the
    only ones that left no trace of having been attempted (review finding on
    PR #37).
    """
    result.failures.append(reason)
    result.lines.append(f"refused: {reason}")
    _finish(stamp, result, execute=execute)
    raise CutoverError(reason)


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
    agents_dir: Path | None = None,
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
    if not checks.ready:
        # A dry run that hides the blockers is worse than no dry run: the
        # rehearsal reports success and the real thing refuses (review
        # finding on PR #22). Blocked checks are reported in both modes and
        # only the execution is refused.
        result.blocked = [check.line() for check in checks.blocked]
        if execute:
            _refuse(
                stamp,
                result,
                execute=execute,
                reason=(
                    "preflight is blocked; run `harrier cutover preflight` and clear it "
                    "first:\n" + "\n".join(result.blocked)
                ),
            )
    if execute and not attested:
        _refuse(
            stamp,
            result,
            execute=execute,
            reason=(
                "the attestations have not been made; re-run with --attested once every "
                "line under `harrier cutover preflight` is true"
            ),
        )

    directory = agents_dir if agents_dir is not None else launch_agents_dir()
    done = set(resume_from(stamp) if execute else ())
    if done:
        result.lines.append(f"resuming: already completed {', '.join(sorted(done))}")
        result.completed.extend(sorted(done))

    quiesce(labels, runner, execute=execute, result=result, agents_dir=agents_dir)
    if result.failures:
        # Stop before touching data: a half-quiesced system with a snapshot
        # taken mid-write is worse than one that never started.
        result.lines.append("stopped after quiesce because a job would not unload")
        _finish(stamp, result, execute=execute)
        return result

    # Everything past this point is covered. The old jobs are down, so any
    # exception here, not only the ones anticipated, must still leave a log
    # and an attempt to put the old system back. Previously an OSError from
    # copytree escaped run_cutover entirely: the log was never written, the
    # rollback never ran, and the operator was left with the old scheduler
    # stopped, nothing installed, no record, and a traceback (spec 037).
    try:
        if STEP_SNAPSHOT not in done:
            snapshot(old_root, snapshot_dir(stamp), execute=execute, result=result)
            if result.failures:
                raise _StepFailed(STEP_SNAPSHOT)
            result.completed.append(STEP_SNAPSHOT)
            _record_progress(stamp, result, execute=execute)

        if STEP_VERIFY not in done:
            verify(conn, result=result)
            if result.failures:
                raise _StepFailed(STEP_VERIFY)
            result.completed.append(STEP_VERIFY)
            _record_progress(stamp, result, execute=execute)

        if install is not None and STEP_INSTALL not in done:
            if not execute:
                result.lines.append("would install the harrier schedule")
            else:
                try:
                    result.lines.extend(install())
                except Exception as error:
                    # Named specifically rather than left to the general
                    # handler below: "schedule install failed" tells the
                    # operator which of the irreversible steps did not
                    # happen, and "RuntimeError after quiesce" does not
                    # (review finding on PR #22, kept through spec 037).
                    result.failures.append(f"schedule install failed: {error}")
                    result.lines.append(f"schedule install FAILED: {error}")
                    raise _StepFailed(STEP_INSTALL) from error
                result.completed.append(STEP_INSTALL)
                _record_progress(stamp, result, execute=execute)
    except _StepFailed as failed:
        result.lines.append(f"stopped at {failed.step}")
        _rollback(result.unloaded, runner, directory, result)
    except Exception as error:
        result.failures.append(f"{type(error).__name__} after quiesce: {error}")
        result.lines.append(f"FAILED after quiesce: {type(error).__name__}: {error}")
        _rollback(result.unloaded, runner, directory, result)

    _finish(stamp, result, execute=execute)
    return result


def utc_stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%d-%H%M")
