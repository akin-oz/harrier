"""The hosted scheduler and its report (spec 108).

`harrier operator scheduler` runs one tick per start and exits. A tick takes
the scheduler's lock, lists every owner's schedule through
`harrier_schedule_inputs()` as `harrier_schedule_reader`, works out which
slots are due, claims each as its owner, starts each run through spec 106's
binding (`harrier_api.runenv.bind_run`), waits for the runs, records how
each ended, and returns. The rules of the schedule and every write to
`schedule_state` are `harrier.hostedschedule`'s; this module is the
process: the start checks, the lock, the queue, the children and the clock.

It sits in `harrier_api` because the binding does, and the domain may not
import the API (`pyproject.toml`, import-linter).

The scheduler never executes domain code as the role that lists owners.
That role, `harrier_schedule_reader`, is switched to for one transaction
that reads one function. Every other transaction runs as `harrier_tenant`
with one owner's claims, the request transaction's shape (spec 104).

The clock, the process spawn and the binding are arguments, so a test runs a
tick against a fake clock, fake processes and a fake binding, and reaches no
network. The real ones are the defaults.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TextIO
from zoneinfo import ZoneInfo

from harrier.connection import (
    URL_VARIABLE,
    Connection,
    StoreError,
    StoreVersionError,
    owner_claims,
)
from harrier.credentials import (
    KEY_SHAPE,
    CredentialsKey,
    CredentialUnreadable,
    StoredCredential,
    parse_key,
    stored_credentials,
)
from harrier.db import data_dir
from harrier.hostedschedule import (
    JOB_NEEDS,
    SQL_I_HOLD_THE_LOCK,
    SQL_LOCK_IS_HELD,
    SQL_TAKE_LOCK,
    DueSlot,
    Outcome,
    OwnerView,
    claim_slot,
    due_slots,
    finish_slot,
    instant_text,
    latest_slot,
    list_inputs,
    load_hosted_jobs,
    outcome_for_exit,
    report_lines,
    sweep_running,
)
from harrier.loginrole import login_role_problems
from harrier.pgstore import check_no_skipped_versions, check_postgres_version, postgres_connect
from harrier.schedule import ScheduleJob
from harrier.sources import scrub_secrets
from harrier.userconfig.store import ConfigError
from harrier_api.hostedauth import (
    RUN_DATABASE_URL_VARIABLE,
    SECRET_KEY_PREFIX,
    SERVICE_ROLE_KEY_VARIABLE,
)
from harrier_api.hostedstore import HostedStartRefused, check_run_login_role
from harrier_api.runenv import BoundRun, ExtraVariableRefused, RunCredentialMissing, bind_run

logger = logging.getLogger(__name__)

CONCURRENCY_VARIABLE = "HARRIER_SCHEDULER_CONCURRENCY"
TIMEOUT_VARIABLE = "HARRIER_SCHEDULED_RUN_TIMEOUT_SECONDS"
CREDENTIALS_KEY_VARIABLE = "HARRIER_CREDENTIALS_KEY"

# How often the loop looks at its children, in seconds.
POLL_SECONDS = 1.0
# How long a terminated child has to exit before it is killed, and how long
# a killed one has to be reaped before the scheduler stops waiting: the run
# manager's grace (`harrier_api.runs.RunManager`, `grace_seconds`).
TERMINATE_GRACE_SECONDS = 5.0
# How often the lock connection is asked whether it still holds the lock.
LOCK_CHECK_SECONDS = 15.0

COMMAND = "harrier operator scheduler"


class StartRefused(RuntimeError):
    """A start check refused the scheduler. The text names the check, and
    never a URL, a password or a value."""

    def __init__(self, check: str) -> None:
        super().__init__(f"{COMMAND} refuses to start: {check}")


# --- the clock, the children, the binding ---


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        import time

        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        import time

        time.sleep(seconds)


class Process(Protocol):
    """A started run, the handle spec 106 leaves the caller: its exit
    status, and a way to stop it."""

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


Spawn = Callable[[Sequence[str], Mapping[str, str], Path], Process]
Binder = Callable[..., BoundRun]


class GroupProcess:
    """A child that leads a process group of its own, so a terminate or a
    kill reaches every process it started and none keeps working after its
    parent is stopped (spec 108, "Finishing a run"; as a hosted run's child
    in `harrier_api.runs`)."""

    def __init__(self, popen: subprocess.Popen[bytes]) -> None:
        self._popen = popen

    def poll(self) -> int | None:
        return self._popen.poll()

    def terminate(self) -> None:
        self._signal(signal.SIGTERM)

    def kill(self) -> None:
        self._signal(signal.SIGKILL)

    def _signal(self, number: int) -> None:
        # A reaped child's pid may be another process's by now.
        if self._popen.returncode is not None:
            return
        with suppress(ProcessLookupError):
            os.killpg(self._popen.pid, number)


def spawn_group(argv: Sequence[str], env: Mapping[str, str], cwd: Path) -> Process:
    """Start `argv` with exactly `env`, in `cwd`, in a session of its own.
    Its output is discarded: nothing a run prints reaches the scheduler's
    logs or the store (spec 108, Open decision 6)."""
    popen = subprocess.Popen(
        list(argv),
        env=dict(env),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return GroupProcess(popen)


# --- settings and start checks ---


@dataclass(frozen=True)
class Settings:
    concurrency: int
    timeout_seconds: int
    # Kept out of the repr: a settings object that reaches a log record
    # carries no password (as `BoundRun` keeps its environment out).
    run_database_url: str = field(repr=False)
    key: CredentialsKey


def _positive(environ: Mapping[str, str], name: str) -> int:
    text = environ.get(name, "").strip()
    if not re.fullmatch(r"[0-9]+", text) or int(text) < 1:
        raise StartRefused(f"{name} is missing or not a positive integer")
    return int(text)


def read_settings(environ: Mapping[str, str]) -> Settings:
    """The scheduler's settings, or a refusal naming the first check that
    fails. Reads the environment only: the checks that need the store come
    after (`check_store`)."""
    for name in sorted(environ):
        if name == SERVICE_ROLE_KEY_VARIABLE or environ[name].strip().startswith(SECRET_KEY_PREFIX):
            raise StartRefused(
                f"the environment holds a service credential in {name}; "
                "the scheduler runs as its own login, never with one"
            )
    # Both limits are required, with no default: what a run may cost in
    # machine time is the operator's decision per environment (spec 110).
    concurrency = _positive(environ, CONCURRENCY_VARIABLE)
    timeout = _positive(environ, TIMEOUT_VARIABLE)
    run_url = environ.get(RUN_DATABASE_URL_VARIABLE, "").strip()
    if not run_url.startswith(("postgresql://", "postgres://")):
        raise StartRefused(
            f"{RUN_DATABASE_URL_VARIABLE} is missing or not a postgresql:// URL for the run "
            "login role; the scheduler binds each run to it"
        )
    key = parse_key(environ.get(CREDENTIALS_KEY_VARIABLE, ""))
    if key is None:
        raise StartRefused(
            f"{CREDENTIALS_KEY_VARIABLE} is missing or not {KEY_SHAPE}; "
            "the scheduler decrypts each owner's credentials to bind a run"
        )
    return Settings(concurrency, timeout, run_url, key)


def check_function_owner(conn: Connection) -> None:
    """Raise `StartRefused` unless the owner of `harrier_schedule_inputs()`
    bypasses row security: a function owned by a role that does not would
    list no owner, so nothing would ever be due, and the report would show
    an empty schedule instead of the fault (spec 108, Failure modes)."""
    owner = conn.execute(
        "SELECT r.rolsuper OR r.rolbypassrls FROM pg_catalog.pg_proc p "
        "JOIN pg_catalog.pg_roles r ON r.oid = p.proowner "
        "WHERE p.proname = 'harrier_schedule_inputs' AND p.pronamespace = 'public'::regnamespace"
    ).fetchone()
    if owner is None:
        raise StartRefused("harrier_schedule_inputs() does not exist; the store is not migrated")
    if not owner[0]:
        raise StartRefused(
            "harrier_schedule_inputs() is owned by a role that does not bypass row security; "
            "it would list no owner"
        )


def check_store(conn: Connection) -> str:
    """The checks that need the store, on the scheduler's own login, in the
    order spec 108 lists them (The scheduler's login). Returns the login
    role's name. Raises `StartRefused` naming the first that fails."""
    row = conn.execute("SELECT current_user::text").fetchone()
    role = str(row[0]) if row else ""
    problems = login_role_problems(conn, role, lists_schedules=True)
    if problems:
        raise StartRefused(f"its login role {role} fails: " + "; ".join(map(str, problems)))
    check_function_owner(conn)
    # Read as the tenant role, which may read the version: the login role
    # holds no grant of its own (spec 134).
    with conn.transaction(tenant=True):
        versions = {int(r[0]) for r in conn.execute("SELECT version FROM public.schema_version")}
    try:
        check_postgres_version(max(versions, default=0))
        check_no_skipped_versions(versions)
    except StoreVersionError as error:
        raise StartRefused(f"the store is not at the version this code needs: {error}") from None
    return role


# --- the lock ---


class AdvisoryLock:
    """The scheduler's session advisory lock, on a dedicated connection that
    holds it for the whole tick (spec 108, "One scheduler at a time")."""

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def try_acquire(self) -> bool:
        row = self._conn.execute(SQL_TAKE_LOCK).fetchone()
        return row is not None and bool(row[0])

    def held(self) -> bool:
        """Whether this connection still holds the lock. False for a
        connection that is gone, and for one that is not a single session
        (a transaction pooler), whose next statement ran elsewhere."""
        try:
            row = self._conn.execute(SQL_I_HOLD_THE_LOCK).fetchone()
        except Exception:
            # Whatever ended the connection, the lock is not ours.
            return False
        return row is not None and bool(row[0])


# --- logging ---

_SAFE_TEXT = (
    StoreError,
    ConfigError,
    CredentialUnreadable,
    RunCredentialMissing,
    ExtraVariableRefused,
)


def describe(error: BaseException) -> str:
    """An exception for a log line: its class, and its text when harrier
    wrote that text. A driver or library error's text can quote a row or a
    path, so only its class is logged (spec 108, "Logs"; spec 135)."""
    if isinstance(error, _SAFE_TEXT):
        return f"{type(error).__name__}: {' '.join(scrub_secrets(str(error)).split())}"
    return type(error).__name__


# --- one tick ---


@dataclass
class _Running:
    entry: DueSlot
    process: Process
    deadline: float
    terminated_at: float | None = None
    killed_at: float | None = None


def _never_stopped() -> bool:
    return False


def _always_held() -> bool:
    return True


@dataclass
class Scheduler:
    """One tick (spec 108, "A tick").

    `connect` opens a connection as the scheduler's login role. The listing
    uses one, switched to `harrier_schedule_reader` for one transaction, and
    every other transaction its own, switched to `harrier_tenant` with one
    owner's claims. Each is closed when its work is done: a connection held
    open through a run of an hour would be one a pooler or a firewall may
    have dropped by the time the outcome is written.
    `lock_held` answers whether the tick still holds its lock, and `stopped`
    whether the process was asked to stop; either ends the tick by
    terminating its children.
    """

    connect: Callable[[], Any]
    jobs: Mapping[str, ScheduleJob]
    settings: Settings
    # The scheduler's whole environment: both database URLs and the
    # credentials key. Never in the repr.
    server_env: Mapping[str, str] = field(repr=False)
    root: Path
    clock: Clock = field(default_factory=SystemClock)
    spawn: Spawn = spawn_group
    bind: Binder = bind_run
    lock_held: Callable[[], bool] = _always_held
    stopped: Callable[[], bool] = _never_stopped
    _queue: list[DueSlot] = field(default_factory=lambda: list[DueSlot](), init=False)
    _running: list[_Running] = field(default_factory=lambda: list[_Running](), init=False)
    _unrecorded: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        # `read_settings` is the one way production builds `Settings`, and it
        # refuses a limit below 1. A scheduler built another way with a limit
        # of 0 would queue slots it never starts and wait for ever.
        if self.settings.concurrency < 1 or self.settings.timeout_seconds < 1:
            raise ValueError("the scheduler's concurrency and run timeout must be at least 1")

    def tick(self) -> int:
        """Run one tick and return the process's exit status: 0 when every
        outcome was recorded, 1 when the listing failed, the lock was lost,
        the process was stopped, or an outcome could not be recorded."""
        try:
            with closing(self.connect()) as driver:
                views = list_inputs(Connection.postgres(driver))
        except Exception as error:
            # The next start lists again (spec 108, "Isolation").
            logger.error("scheduler could not list schedules: %s", describe(error))
            return 1
        views = self._sweep(views)
        self._queue = due_slots(views, self.jobs, self.clock.now())
        status = self._drive()
        return 1 if self._unrecorded and status == 0 else status

    # --- the owner's transaction ---

    @contextmanager
    def _as_owner(self, owner: str) -> Generator[Connection]:
        with closing(self.connect()) as driver:
            conn = Connection.postgres(driver, claims=owner_claims(owner))
            with conn.transaction():
                yield conn

    # --- sweeping ---

    def _sweep(self, views: list[OwnerView]) -> list[OwnerView]:
        """Mark every `running` row `interrupted`, owner by owner, each in a
        transaction as that owner: the scheduler that started them is gone.
        An owner whose rows cannot be swept keeps them `running`, so none of
        their slots is started this tick."""
        for view in views:
            if not any(state.last_outcome == "running" for state in view.states.values()):
                continue
            finished = instant_text(self.clock.now())
            try:
                with self._as_owner(view.owner) as conn:
                    sweep_running(conn, finished_at=finished)
            except Exception as error:
                logger.error(
                    "scheduled step failed owner=%s step=sweep error=%s",
                    view.owner,
                    describe(error),
                )
                continue
            for name, state in list(view.states.items()):
                if state.last_outcome == "running":
                    view.states[name] = replace(
                        state, last_outcome="interrupted", last_finished_at=finished, last_exit=None
                    )
        return views

    # --- the loop ---

    def _drive(self) -> int:
        next_lock_check = self.clock.monotonic() + LOCK_CHECK_SECONDS
        while self._queue or self._running:
            if self.stopped():
                self._abandon("scheduler stopped")
                return 1
            if self.clock.monotonic() >= next_lock_check:
                next_lock_check = self.clock.monotonic() + LOCK_CHECK_SECONDS
                if not self.lock_held():
                    self._abandon("scheduler lost its lock")
                    return 1
            self._fill()
            self._reap()
            if self._queue or self._running:
                self.clock.sleep(POLL_SECONDS)
        return 0

    def _busy_owners(self) -> set[str]:
        return {running.entry.owner for running in self._running}

    def _fill(self) -> None:
        """Start queued slots while fewer than the concurrency limit are
        running. At most one scheduled run per owner runs at a time, so an
        owner's second slot waits for their first (spec 108, "Isolation")."""
        while len(self._running) < self.settings.concurrency:
            busy = self._busy_owners()
            entry = next((e for e in self._queue if e.owner not in busy), None)
            if entry is None:
                return
            self._queue.remove(entry)
            self._launch(entry)

    def _enqueue(self, entry: DueSlot) -> None:
        self._queue.append(entry)
        self._queue.sort(key=DueSlot.order)

    # --- claiming and starting ---

    def _launch(self, entry: DueSlot) -> None:
        """Recheck, claim, and start one slot as its owner (spec 108,
        "Claiming and starting a run as its owner"). Every step catches
        every exception: a failure here is this owner's, and the loop goes
        on."""
        job = self.jobs[entry.job]
        now = self.clock.now()
        stored: list[StoredCredential] = []
        newer: DueSlot | None = None
        claimed = False
        try:
            latest = latest_slot(job, ZoneInfo(entry.timezone), now)
            with self._as_owner(entry.owner) as conn:
                if latest is not None and latest > entry.at:
                    # The slot waited past the next one: it is recorded
                    # `missed` and the newer slot is queued instead.
                    claim_slot(
                        conn, entry.job, entry.text, started_at=instant_text(now), outcome="missed"
                    )
                    newer = replace(entry, at=latest)
                else:
                    claimed = claim_slot(conn, entry.job, entry.text, started_at=instant_text(now))
                    if claimed:
                        stored = stored_credentials(conn)
        except Exception as error:
            logger.error(
                "scheduled step failed owner=%s job=%s step=claim error=%s",
                entry.owner,
                entry.job,
                describe(error),
            )
            return
        if newer is not None:
            self._log_end(entry, "missed", None)
            self._enqueue(newer)
            return
        if not claimed:
            return
        try:
            bound = self.bind(
                server_env=self.server_env,
                owner=entry.owner,
                root=self.root,
                stored=stored,
                key=self.settings.key,
                needs=JOB_NEEDS.get(entry.job, ()),
                extra={"TZ": entry.timezone},
            )
        except RunCredentialMissing:
            self._record(entry, "not_configured", None)
            return
        except Exception as error:
            # An unreadable credential also names its key id, as spec 106
            # logs it: an operator needs it to see a replaced key. The key
            # id is plain text, never a value.
            detail = describe(error)
            if isinstance(error, CredentialUnreadable):
                detail += f" key_id={error.key_id}"
            logger.error(
                "scheduled step failed owner=%s job=%s step=bind error=%s",
                entry.owner,
                entry.job,
                detail,
            )
            self._record(entry, "refused", None)
            return
        argv = [sys.executable, "-m", "harrier_cli.main", *job.command]
        try:
            process = self.spawn(argv, bound.env, bound.cwd)
        except Exception as error:
            logger.error(
                "scheduled step failed owner=%s job=%s step=spawn error=%s",
                entry.owner,
                entry.job,
                describe(error),
            )
            self._record(entry, "crashed", None)
            return
        logger.info("scheduled start owner=%s job=%s slot=%s", entry.owner, entry.job, entry.text)
        deadline = self.clock.monotonic() + self.settings.timeout_seconds
        self._running.append(_Running(entry, process, deadline))

    # --- waiting ---

    def _reap(self) -> None:
        for run in list(self._running):
            now = self.clock.monotonic()
            code = run.process.poll()
            if code is not None:
                if run.terminated_at is not None:
                    self._finish(run, "timed_out", None)
                else:
                    outcome, exit_code = outcome_for_exit(code)
                    self._finish(run, outcome, exit_code)
            elif run.killed_at is not None:
                # Killed and not reaped within the grace: stop waiting. The
                # row says `timed_out`; the process is outside the scheduler's reach.
                if now - run.killed_at >= TERMINATE_GRACE_SECONDS:
                    logger.error(
                        "scheduled step failed owner=%s job=%s step=kill error=not reaped",
                        run.entry.owner,
                        run.entry.job,
                    )
                    self._finish(run, "timed_out", None)
            elif run.terminated_at is not None:
                if now - run.terminated_at >= TERMINATE_GRACE_SECONDS:
                    run.process.kill()
                    run.killed_at = now
            elif now >= run.deadline:
                # Stopped, so its remote work stops charging where the run's
                # own code handles termination (spec 097).
                run.process.terminate()
                run.terminated_at = now

    def _abandon(self, reason: str) -> None:
        """End the tick early: terminate every child still running, kill
        the ones that outlast the grace, and record each `interrupted`
        (spec 108, "One scheduler at a time"). A child that had already
        finished keeps its true outcome, because a finish writes only its
        own slot."""
        logger.error(reason)
        self._queue.clear()
        for run in list(self._running):
            code = run.process.poll()
            if code is None:
                run.process.terminate()
            else:
                outcome, exit_code = outcome_for_exit(code)
                self._finish(run, outcome, exit_code)
        waited = 0.0
        while self._running and waited < TERMINATE_GRACE_SECONDS:
            self.clock.sleep(POLL_SECONDS)
            waited += POLL_SECONDS
            for run in list(self._running):
                if run.process.poll() is not None:
                    self._finish(run, "interrupted", None)
        for run in list(self._running):
            run.process.kill()
        if self._running:
            self.clock.sleep(POLL_SECONDS)
        for run in list(self._running):
            self._finish(run, "interrupted", None)

    # --- recording ---

    def _finish(self, run: _Running, outcome: Outcome, exit_code: int | None) -> None:
        """Record how `run` ended. A run the scheduler already terminated
        for its timeout is `timed_out`, whatever ends the wait after: the
        status a terminated child exits with is the scheduler's own signal,
        and a lost lock or a stop that arrives before the child is reaped
        does not make it `crashed` or `interrupted` (review of PR #327)."""
        if run.terminated_at is not None:
            outcome, exit_code = "timed_out", None
        self._running.remove(run)
        self._record(run.entry, outcome, exit_code)

    def _record(self, entry: DueSlot, outcome: Outcome, exit_code: int | None) -> None:
        now = instant_text(self.clock.now())
        try:
            with self._as_owner(entry.owner) as conn:
                changed = finish_slot(
                    conn,
                    entry.job,
                    entry.text,
                    outcome=outcome,
                    exit_code=exit_code,
                    finished_at=now,
                    ok_at=now if outcome == "ok" else None,
                )
            if not changed:
                # The owner's row is gone, or holds a newer slot: nothing
                # recorded this outcome, so the tick is not a clean one.
                raise LookupError("no schedule_state row holds this slot")
        except Exception as error:
            self._unrecorded += 1
            logger.error(
                "scheduled step failed owner=%s job=%s step=finish error=%s",
                entry.owner,
                entry.job,
                describe(error),
            )
            return
        self._log_end(entry, outcome, exit_code)

    @staticmethod
    def _log_end(entry: DueSlot, outcome: str, exit_code: int | None) -> None:
        logger.info(
            "scheduled end owner=%s job=%s slot=%s outcome=%s exit=%s",
            entry.owner,
            entry.job,
            entry.text,
            outcome,
            "-" if exit_code is None else exit_code,
        )


# --- the process ---


def _refuse(error: BaseException) -> int:
    print(f"error: {error}", file=sys.stderr)
    return 1


def run_scheduler(
    environ: Mapping[str, str],
    *,
    clock: Clock | None = None,
    spawn: Spawn = spawn_group,
    bind: Binder = bind_run,
    jobs: Mapping[str, ScheduleJob] | None = None,
    root: Path | None = None,
    stop: threading.Event | None = None,
    handle_signals: bool = False,
) -> int:
    """`harrier operator scheduler`: the start checks, the lock, one tick.

    Exit 0 when the tick ran, or when another instance holds the lock and
    this one stood by. Exit 1 when a start check refused, the lock was lost,
    the process was stopped, or the listing failed; the next scheduled start
    is the retry (spec 108, "What one start does").
    """
    try:
        settings = read_settings(environ)
        url = environ.get(URL_VARIABLE, "")
        stopping = stop if stop is not None else threading.Event()
        with closing(postgres_connect(url)) as checker:
            role = check_store(Connection.postgres(checker))
        with closing(postgres_connect(url)) as locker:
            try:
                check_run_login_role(settings.run_database_url, role)
            except HostedStartRefused as refused:
                # The check is the API's; the words are the scheduler's.
                raise StartRefused(
                    str(refused)
                    .replace("the hosted API", "the scheduler")
                    .replace("the API's own login role", "the scheduler's own login role")
                ) from None
            lock = AdvisoryLock(Connection.postgres(locker))
            if not lock.try_acquire():
                logger.info("scheduler standing by: another instance holds the lock")
                return 0
            if not lock.held():
                raise StartRefused(
                    "the database URL does not keep a session advisory lock; name a direct or "
                    "session-mode connection, not a transaction pooler"
                )
            previous = _install_signal_handlers(stopping) if handle_signals else {}
            try:
                scheduler = Scheduler(
                    connect=lambda: postgres_connect(url),
                    jobs=jobs if jobs is not None else load_hosted_jobs(),
                    settings=settings,
                    server_env=environ,
                    root=root if root is not None else data_dir(),
                    clock=clock if clock is not None else SystemClock(),
                    spawn=spawn,
                    bind=bind,
                    lock_held=lock.held,
                    stopped=stopping.is_set,
                )
                return scheduler.tick()
            finally:
                _restore_signal_handlers(previous)
    except (StartRefused, StoreError) as error:
        return _refuse(error)


def _install_signal_handlers(stopping: threading.Event) -> dict[int, Any]:
    """SIGINT and SIGTERM ask the tick to stop: Fly sends both, a second
    apart, when an update replaces the machine under a running tick
    (spec 108, Amendment (2026-10-12))."""
    previous: dict[int, Any] = {}
    for number in (signal.SIGINT, signal.SIGTERM):
        previous[number] = signal.signal(number, lambda _n, _f: stopping.set())
    return previous


def _restore_signal_handlers(previous: Mapping[int, Any]) -> None:
    for number, handler in previous.items():
        signal.signal(number, handler)


# --- the report ---


def run_report(
    environ: Mapping[str, str],
    *,
    clock: Clock | None = None,
    jobs: Mapping[str, ScheduleJob] | None = None,
    out: TextIO | None = None,
) -> int:
    """`harrier operator schedule-report`: whether a tick is running, and one
    line per owner and job. Exit 1 when any line names a problem, 0
    otherwise (spec 108, "The operator's report")."""
    stream = out if out is not None else sys.stdout
    try:
        with closing(postgres_connect(environ.get(URL_VARIABLE, ""))) as driver:
            conn = Connection.postgres(driver)
            # A listing that would silently be empty is the fault the report
            # exists to show, so it is refused, not printed as a quiet one.
            check_function_owner(conn)
            views = list_inputs(conn)
            row = conn.execute(SQL_LOCK_IS_HELD).fetchone()
            running = row is not None and bool(row[0])
    except (StartRefused, StoreError) as error:
        return _refuse(error)
    now = (clock if clock is not None else SystemClock()).now()
    lines = report_lines(views, jobs if jobs is not None else load_hosted_jobs(), now)
    print("scheduler: a tick is running" if running else "scheduler: no tick running", file=stream)
    for line in lines:
        print(line.text, file=stream)
    return 1 if any(line.problem for line in lines) else 0
