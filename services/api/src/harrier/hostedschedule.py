"""The hosted schedule (spec 108).

On the hosted store an owner chooses which scheduled jobs run for them and
in which timezone, as the `schedule` kind of their configuration. Every
owner shares the committed cadence in `config/schedule.json`; the local
product keeps launchd and `harrier schedule` (ADR-006).

`HOSTED_JOBS` names the jobs a hosted owner may choose. `digest` joins when
spec 109 gives owners their own Telegram settings, and `gmail-watch` when a
hosted Gmail spec exists.

This module is the schedule's rules and its writes, and none of the process:

- the slot a job is due at, in its owner's timezone, and which slots are
  due given what the store holds (`latest_slot`, `due_slots`);
- the one cross-owner read, `harrier_schedule_inputs()` as
  `harrier_schedule_reader` (`list_inputs`), and the writes that follow it,
  each in a transaction as one owner (`claim_slot`, `finish_slot`,
  `sweep_running`);
- the outcome classes (`outcome_for_exit`) and the operator's report
  (`report_lines`).

It names no tenant in SQL (ADR-013 decision 2): a write sees only the rows
of the owner whose connection it runs on. The process that lists, queues,
starts runs and waits for them is `harrier_api.hostedscheduler`, which sits
beside the run binding it uses.

Times are `datetime` in UTC. Stored times are ISO 8601 UTC text,
`YYYY-MM-DDTHH:MM:SSZ`; the owner's choice carries `updated_at` as
`store_now()` writes it, `YYYY-MM-DD HH:MM:SS`. The two order wrongly as
text on one day (a space sorts before `T`), so every comparison of a time
parses both first (`parse_instant`).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Literal, cast, get_args
from zoneinfo import ZoneInfo

from harrier.connection import (
    Connection,
    StoreIntegrityError,
    is_driver_error,
    store_error_from_postgres,
    update_or_insert,
)
from harrier.credentials import CredentialKind
from harrier.paths import repo_root
from harrier.schedule import (
    OVERDUE_GAPS,
    SCHEDULE_CONFIG_PATH,
    ScheduleJob,
    describe_cadence,
    load_schedule,
    longest_gap,
)
from harrier.tracker.schema import SCHEDULE_READER_ROLE

logger = logging.getLogger(__name__)

HOSTED_JOBS: tuple[str, ...] = ("discovery", "academic-discovery")

# The credential kinds a job's binding refuses without, so a run that could
# not work is recorded `not_configured` and not started (spec 108, Failure
# modes). Discovery runs without Apify: the feed sources need no token, and
# the run manager's table says the same of its `discovery` kind
# (`harrier_api.runs.HOSTED_RUN_KINDS`). The academic source is Apify only.
JOB_NEEDS: dict[str, tuple[CredentialKind, ...]] = {
    "discovery": (),
    "academic-discovery": ("apify",),
}

Outcome = Literal[
    "running",
    "ok",
    "failed",
    "crashed",
    "timed_out",
    "not_configured",
    "refused",
    "interrupted",
    "missed",
]
OUTCOMES: tuple[str, ...] = get_args(Outcome)

# `harrier_cli` exits 3 when every attempted source failed or none was
# attempted (`harrier.runoutcome.EXIT_RUN_FAILED`, spec 029).
EXIT_TOTAL_FAILURE = 3

# Outcomes that need the operator (spec 108, "The operator's report").
PROBLEM_OUTCOMES: frozenset[str] = frozenset({"failed", "crashed", "timed_out", "refused"})

# A slot is started by the first tick at or after it. Fly starts the tick on
# an hourly cycle it calls approximate, and skips a start that falls while a
# tick runs, so a slot may start about two hours late (spec 108, Amendment
# (2026-10-12), "Timing, answered"). A slot still unclaimed this long after
# its time is the scheduler not keeping up or not running. Akin decided the
# 3 hours on 2026-10-14, an hour of margin on the two; it is not measured
# (spec 108, Amendment (2026-10-14)).
UNCLAIMED_AFTER = timedelta(hours=3)

# --- time ---


def instant_text(moment: datetime) -> str:
    """`moment` as the stored text, `2026-10-12T07:00:00Z`."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_instant(text: str) -> datetime | None:
    """A stored time as a UTC `datetime`: the `T` and `Z` form, or the
    `store_now()` form with a space and no zone, which is UTC too. None for
    text that is neither."""
    try:
        moment = datetime.fromisoformat(text.strip().replace(" ", "T").replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def latest_slot(job: ScheduleJob, zone: tzinfo, now: datetime) -> datetime | None:
    """The job's latest slot at or before `now`, in UTC, with the cadence
    read in `zone` (spec 108, "The slot").

    A cadence time is a wall-clock time on a calendar day of the owner. A
    local time that does not exist on a day the clocks go forward is the
    instant `zoneinfo` gives for `fold=0`; a local time that occurs twice
    is the `fold=0` instant, so it is one slot. `weekday` is launchd's
    numbering, 1 for Monday, which is `isoweekday`. Nine days of candidates
    reach a weekly job's previous slot whatever the zone's offset.
    """
    if job.kind != "calendar":
        raise ValueError(f"{job.name} is not a calendar job")
    today = now.astimezone(zone).date()
    best: datetime | None = None
    for back in range(-1, 9):
        day = today - timedelta(days=back)
        for time in job.times:
            if time.weekday is not None and day.isoweekday() != time.weekday:
                continue
            local = datetime(day.year, day.month, day.day, time.hour, time.minute, tzinfo=zone)
            instant = local.astimezone(UTC)
            if instant <= now and (best is None or instant > best):
                best = instant
    return best


# --- the committed cadence ---


def load_hosted_jobs(path: Path | None = None) -> dict[str, ScheduleJob]:
    """The hosted jobs, as `config/schedule.json` defines them, by name.

    A job of `HOSTED_JOBS` the file does not name, or names as an interval,
    is skipped with a log line (spec 108, "The hosted job set"): the
    scheduler runs the jobs it can and says which it cannot.
    """
    config = path if path is not None else repo_root() / SCHEDULE_CONFIG_PATH
    _prefix, jobs = load_schedule(config)
    by_name = {job.name: job for job in jobs}
    hosted: dict[str, ScheduleJob] = {}
    for name in HOSTED_JOBS:
        job = by_name.get(name)
        if job is None or job.kind != "calendar":
            logger.warning("scheduled skip job=%s reason=not a calendar job in the schedule", name)
            continue
        hosted[name] = job
    return hosted


# --- what the store holds ---


@dataclass(frozen=True)
class JobState:
    """One `schedule_state` row as the listing returns it."""

    last_slot_at: str
    last_started_at: str | None
    last_finished_at: str | None
    last_outcome: str
    last_exit: int | None
    last_ok_at: str | None


@dataclass
class OwnerView:
    """One owner as the listing shows them: their `schedule` row, when they
    have one, and a state row per job."""

    owner: str
    schedule: str | None = None
    schedule_updated_at: str | None = None
    states: dict[str, JobState] = field(default_factory=lambda: dict[str, JobState]())


def list_inputs(driver: Any) -> list[OwnerView]:
    """Every owner's schedule choice and scheduler state, through
    `harrier_schedule_inputs()` (spec 108, "A tick", step 1).

    In a transaction that runs `SET LOCAL ROLE harrier_schedule_reader`
    first and sets no claims, and runs nothing else: the one read across
    owners. Owners are returned in id order.

    `driver` is the scheduler's own psycopg connection, not a `Connection`:
    the seam's `execute()` refuses a statement that changes the session, and
    its `transaction()` can take the tenant role and no other (spec 112,
    amendment of 2026-10-14: the session). This is the one statement
    sequence the scheduler sends past the seam, the reader's role switch and
    the listing, in one transaction that is rolled back since it writes
    nothing (spec 108, Amendment (2026-10-14): the session). A driver error
    is raised as the seam would raise it.
    """
    try:
        with driver.transaction(force_rollback=True):
            driver.execute(f"SET LOCAL ROLE {SCHEDULE_READER_ROLE}".encode())
            # Every column, in the order the function declares them (owner,
            # schedule, schedule_updated_at, job, last_slot_at,
            # last_started_at, last_finished_at, last_outcome, last_exit,
            # last_ok_at), so this module's SQL names no tenant column.
            rows = driver.execute(b"SELECT * FROM public.harrier_schedule_inputs()").fetchall()
    except Exception as error:
        if is_driver_error(error):
            raise store_error_from_postgres(cast(Any, error)) from error
        raise
    views: dict[str, OwnerView] = {}
    for row in rows:
        owner = str(row[0])
        view = views.setdefault(owner, OwnerView(owner))
        if row[1] is not None:
            view.schedule = str(row[1])
            view.schedule_updated_at = None if row[2] is None else str(row[2])
        if row[3] is not None:
            view.states[str(row[3])] = JobState(
                last_slot_at=str(row[4]),
                last_started_at=None if row[5] is None else str(row[5]),
                last_finished_at=None if row[6] is None else str(row[6]),
                last_outcome=str(row[7]),
                last_exit=None if row[8] is None else int(row[8]),
                last_ok_at=None if row[9] is None else str(row[9]),
            )
    return [views[owner] for owner in sorted(views)]


@dataclass(frozen=True)
class Choice:
    timezone: str
    jobs: tuple[str, ...]


def read_choice(text: str) -> Choice:
    """An owner's stored `schedule` value, validated as the store validates
    a write. Raises `ConfigError` naming the field, quoting nothing the
    owner wrote."""
    import json

    from harrier.userconfig.store import ConfigError, validate_schedule

    try:
        parsed: object = json.loads(text)
    except (ValueError, RecursionError):
        # A value nested past the parser's depth raises RecursionError, and
        # one with too many digits ValueError; neither is a reason to stop
        # the tick for every other owner.
        raise ConfigError("schedule is not valid JSON") from None
    value = validate_schedule(parsed)
    return Choice(timezone=str(value["timezone"]), jobs=tuple(cast("list[str]", value["jobs"])))


# --- which slots are due ---


@dataclass(frozen=True)
class DueSlot:
    """A slot of one owner's job that is due and not yet claimed."""

    owner: str
    job: str
    at: datetime
    timezone: str
    # The state row's last start, for the queue's order; None when the job
    # has never started for this owner.
    last_started_at: str | None

    @property
    def text(self) -> str:
        return instant_text(self.at)

    def order(self) -> tuple[datetime, int, str, str, str]:
        """Slot time, then never-started first, then the earlier last start,
        then owner id (spec 108, "A tick", step 3). The job name only makes
        the order total."""
        started = self.last_started_at or ""
        return (self.at, 0 if not started else 1, started, self.owner, self.job)


def _zone(name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError, OSError):
        # ZoneInfoNotFoundError is a KeyError; a directory of the tz
        # database is an OSError on some systems.
        return None


def _due_for(view: OwnerView, jobs: Mapping[str, ScheduleJob], now: datetime) -> list[DueSlot]:
    """One owner's due slots (`due_slots`)."""
    from harrier.userconfig.store import ConfigError

    due: list[DueSlot] = []
    if view.schedule is None:
        return due
    try:
        choice = read_choice(view.schedule)
    except ConfigError as error:
        logger.warning("scheduled skip owner=%s reason=%s", view.owner, error)
        return due
    zone = _zone(choice.timezone)
    if zone is None:
        logger.warning(
            "scheduled skip owner=%s reason=schedule.timezone is not a zone this server can load",
            view.owner,
        )
        return due
    if any(state.last_outcome == "running" for state in view.states.values()):
        return due
    chosen_at = (
        None if view.schedule_updated_at is None else parse_instant(view.schedule_updated_at)
    )
    if view.schedule_updated_at is not None and chosen_at is None:
        logger.warning(
            "scheduled skip owner=%s reason=schedule_updated_at is not a time", view.owner
        )
        return due
    for name in choice.jobs:
        job = jobs.get(name)
        if job is None:
            continue
        slot = latest_slot(job, zone, now)
        if slot is None:
            continue
        # A slot before the owner's choice is not theirs: enabling a
        # job at 15:00 starts nothing until 16:00.
        if chosen_at is not None and slot < chosen_at:
            continue
        state = view.states.get(name)
        if state is not None:
            claimed = parse_instant(state.last_slot_at)
            if claimed is None:
                logger.warning(
                    "scheduled skip owner=%s job=%s reason=schedule_state.last_slot_at "
                    "is not a time",
                    view.owner,
                    name,
                )
                continue
            if claimed >= slot:
                continue
        due.append(
            DueSlot(
                view.owner,
                name,
                slot,
                choice.timezone,
                None if state is None else state.last_started_at,
            )
        )
    return due


def due_slots(
    views: Iterable[OwnerView], jobs: Mapping[str, ScheduleJob], now: datetime
) -> list[DueSlot]:
    """The slots due at `now` (spec 108, "A tick", step 2): for each owner
    with a valid `schedule` row and each job in it, the job's latest slot,
    when it is later than the state's `last_slot_at`, at or after the row's
    `updated_at`, and the owner has no scheduled run in progress. After a
    gap only the latest slot of each job is due, so an outage never
    multiplies an owner's runs.

    A row that fails validation is skipped and logged with the owner's id
    and the field, never the value.
    """
    due: list[DueSlot] = []
    for view in views:
        try:
            due.extend(_due_for(view, jobs, now))
        except Exception as error:
            # Every per-owner step catches every exception (spec 108,
            # "Isolation"): a row nothing foresaw starts nothing for its
            # owner and stops nobody else. Logged by class, never text.
            logger.warning(
                "scheduled skip owner=%s reason=schedule could not be read: %s",
                view.owner,
                type(error).__name__,
            )
    return sorted(due, key=DueSlot.order)


# --- the writes, each on a connection that is one owner's ---


def claim_slot(
    conn: Connection,
    job: str,
    slot: str,
    *,
    started_at: str,
    outcome: Literal["running", "missed"] = "running",
) -> bool:
    """Claim `slot` of `job` for the connection's owner, in the caller's
    transaction. False when the row already holds this slot or a later one:
    the slot is someone else's, and nothing starts (spec 108, "Claiming and
    starting a run as its owner", step 2).

    `running` is the claim before a start; `started_at` is its start.
    `missed` records a slot that waited past the next one: it was never
    started, so it changes no start, and it is finished at `started_at` as it
    is claimed.
    """
    try:
        if outcome == "missed":
            # Nothing started, so `last_started_at` stays what it was: the
            # queue orders owners by it, and a missed slot must not make an
            # owner look never-started.
            update_or_insert(
                conn,
                "UPDATE schedule_state SET last_slot_at = ?, last_finished_at = ?, "
                "last_outcome = 'missed', last_exit = NULL "
                "WHERE job = ? AND last_slot_at < ?",
                (slot, started_at, job, slot),
                "INSERT INTO schedule_state (job, last_slot_at, last_finished_at, last_outcome) "
                "VALUES (?, ?, ?, 'missed')",
                (job, slot, started_at),
            )
        else:
            update_or_insert(
                conn,
                "UPDATE schedule_state SET last_slot_at = ?, last_started_at = ?, "
                "last_finished_at = NULL, last_outcome = 'running', last_exit = NULL "
                "WHERE job = ? AND last_slot_at < ?",
                (slot, started_at, job, slot),
                "INSERT INTO schedule_state (job, last_slot_at, last_started_at, last_outcome) "
                "VALUES (?, ?, ?, 'running')",
                (job, slot, started_at),
            )
    except StoreIntegrityError as error:
        if error.kind != "unique":
            raise
        return False
    return True


def finish_slot(
    conn: Connection,
    job: str,
    slot: str,
    *,
    outcome: Outcome,
    exit_code: int | None,
    finished_at: str,
    ok_at: str | None = None,
) -> bool:
    """Record how the run of `slot` ended, for the connection's owner. The
    `last_slot_at` condition means a finish never overwrites a later slot's
    claim; False when it changed no row. `ok_at` is set only on `ok`, the
    rule `record_success` keeps for `job_runs` (spec 108, "Finishing a
    run")."""
    if ok_at is None:
        cursor = conn.execute(
            "UPDATE schedule_state SET last_finished_at = ?, last_outcome = ?, last_exit = ? "
            "WHERE job = ? AND last_slot_at = ?",
            (finished_at, outcome, exit_code, job, slot),
        )
    else:
        cursor = conn.execute(
            "UPDATE schedule_state SET last_finished_at = ?, last_outcome = ?, last_exit = ?, "
            "last_ok_at = ? WHERE job = ? AND last_slot_at = ?",
            (finished_at, outcome, exit_code, ok_at, job, slot),
        )
    return cursor.rowcount > 0


def sweep_running(conn: Connection, *, finished_at: str) -> int:
    """Mark every `running` row of the connection's owner `interrupted`: a
    scheduler that starts holding the lock found them, so the process that
    ran them is gone (spec 108, "Finishing a run"). The slots are not
    started again. Returns how many rows changed."""
    cursor = conn.execute(
        "UPDATE schedule_state SET last_outcome = 'interrupted', last_finished_at = ?, "
        "last_exit = NULL WHERE last_outcome = 'running'",
        (finished_at,),
    )
    return cursor.rowcount


def outcome_for_exit(code: int) -> tuple[Outcome, int]:
    """The outcome class of a finished run's exit status (spec 108,
    "Finishing a run"): 0 is `ok`, 3 is spec 029's total failure, and any
    other status, a signal's negative number included, is `crashed`."""
    if code == 0:
        return "ok", 0
    if code == EXIT_TOTAL_FAILURE:
        return "failed", code
    return "crashed", code


# --- the operator's report ---


@dataclass(frozen=True)
class ReportLine:
    owner: str
    job: str
    text: str
    # What needs the operator, or "" when nothing does.
    problem: str


def _age(since: datetime, now: datetime) -> str:
    hours = int((now - since).total_seconds() // 3600)
    return f"{hours // 24}d{hours % 24}h" if hours >= 24 else f"{hours}h"


def _problems(
    job: ScheduleJob,
    state: JobState | None,
    chosen_at: datetime | None,
    zone: tzinfo | None,
    now: datetime,
) -> list[str]:
    found: list[str] = []
    if state is not None and state.last_outcome in PROBLEM_OUTCOMES:
        found.append(f"last run {state.last_outcome}")
    limit = longest_gap(job) * OVERDUE_GAPS
    ok_at = None if state is None or state.last_ok_at is None else parse_instant(state.last_ok_at)
    if ok_at is not None:
        if now - ok_at > limit:
            found.append(f"overdue: last ok {_age(ok_at, now)} ago")
    elif chosen_at is not None and now - chosen_at > limit:
        found.append(f"overdue: never ok since chosen {_age(chosen_at, now)} ago")
    if zone is not None:
        slot = latest_slot(job, zone, now)
        claimed = None if state is None else parse_instant(state.last_slot_at)
        if (
            slot is not None
            and (chosen_at is None or slot >= chosen_at)
            and (claimed is None or claimed < slot)
            and now - slot > UNCLAIMED_AFTER
        ):
            found.append(f"a whole slot passed unclaimed: {instant_text(slot)}")
    return found


def _report_for(
    view: OwnerView, jobs: Mapping[str, ScheduleJob], now: datetime
) -> list[ReportLine]:
    """One owner's report lines (`report_lines`)."""
    from harrier.userconfig.store import ConfigError

    lines: list[ReportLine] = []
    chosen_at = (
        None if view.schedule_updated_at is None else parse_instant(view.schedule_updated_at)
    )
    choice: Choice | None = None
    invalid = ""
    if view.schedule is not None:
        try:
            choice = read_choice(view.schedule)
        except ConfigError as error:
            invalid = str(error)
    zone = None if choice is None else _zone(choice.timezone)
    # An invalid row names no jobs, so the jobs its owner has state for
    # stand in for them: they become overdue, as the spec says.
    chosen: set[str] = set()
    if choice is not None:
        chosen = set(choice.jobs)
    elif invalid:
        chosen = set(view.states)
    if invalid and not view.states:
        lines.append(ReportLine(view.owner, "-", f"{view.owner} - schedule invalid: {invalid}", ""))
    for name in sorted(chosen | set(view.states)):
        job = jobs.get(name)
        state = view.states.get(name)
        cadence = "-" if job is None else describe_cadence(job)
        outcome = "none" if state is None else state.last_outcome
        exit_text = "-" if state is None or state.last_exit is None else str(state.last_exit)
        slot_text = "-" if state is None else state.last_slot_at
        ok_text = "-" if state is None or not state.last_ok_at else state.last_ok_at
        text = (
            f"{view.owner} {name} cadence={cadence!r} slot={slot_text} "
            f"outcome={outcome} exit={exit_text} ok={ok_text}"
        )
        problems: list[str] = []
        if name not in chosen:
            text += " (not chosen)"
        elif job is not None:
            problems = _problems(job, state, chosen_at, zone, now)
        if invalid:
            text += f" (schedule invalid: {invalid})"
        problem = "; ".join(problems)
        lines.append(
            ReportLine(
                view.owner, name, f"{text}  PROBLEM: {problem}" if problem else text, problem
            )
        )
    return lines


def report_lines(
    views: Iterable[OwnerView], jobs: Mapping[str, ScheduleJob], now: datetime
) -> list[ReportLine]:
    """One line per owner and job (spec 108, "The operator's report"): the
    owner's id, the job, its cadence in words, the last slot, outcome, exit
    and ok time, and a problem when there is one.

    A problem is a last outcome of `failed`, `crashed`, `timed_out` or
    `refused`; a job whose last `ok`, or the owner's choice of it, is older
    than twice its longest gap (spec 029's rule); or a slot that passed
    unclaimed (`UNCLAIMED_AFTER`). `not_configured`, `interrupted` and
    `missed` are shown and are not problems on their own. Only a job the
    owner has chosen can be a problem, so an owner who stopped a job is not
    reported for its last failure.
    """
    lines: list[ReportLine] = []
    for view in views:
        try:
            lines.extend(_report_for(view, jobs, now))
        except Exception as error:
            # The report is how an operator finds a row the scheduler
            # cannot read, so it shows one it cannot read either.
            lines.append(
                ReportLine(
                    view.owner,
                    "-",
                    f"{view.owner} - schedule could not be read: {type(error).__name__}",
                    "",
                )
            )
    return lines


# --- the scheduler's lock ---

# The two-key form of a session advisory lock, `hashtext('harrier.scheduler')`
# and 0, so it never meets spec 103's one-key migration lock. `pg_locks`
# shows the first key as an unsigned `classid`.
SQL_TAKE_LOCK = (
    "SELECT pg_catalog.pg_try_advisory_lock(pg_catalog.hashtext('harrier.scheduler'), 0)"
)
_LOCK_ROW = (
    "locktype = 'advisory' AND granted AND objsubid = 2 AND objid = 0 "
    "AND classid::bigint = (pg_catalog.hashtext('harrier.scheduler')::bigint & 4294967295) "
    # Advisory locks belong to a database, and pg_locks lists the cluster's.
    "AND database = (SELECT oid FROM pg_catalog.pg_database "
    "WHERE datname = pg_catalog.current_database())"
)
# Whether any session holds it: the report's question.
SQL_LOCK_IS_HELD = f"SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_locks WHERE {_LOCK_ROW})"
# Whether this session does: the scheduler's, which is also how it learns
# its connection is gone, or is not one session (a transaction pooler).
SQL_I_HOLD_THE_LOCK = (
    f"SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_locks WHERE {_LOCK_ROW} "
    "AND pid = pg_catalog.pg_backend_pid())"
)
