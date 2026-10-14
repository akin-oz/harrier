"""The hosted scheduler and its report (spec 108, the scheduler's pull request).

A tick runs against a copy of the migrated template, as a login role made for
the test: a member of `harrier_tenant` and `harrier_schedule_reader` and of
nothing else, as spec 108 describes `harrier_scheduler`. The clock is fake
and the runs are fake processes the test steers, so a tick of hours takes
milliseconds; the few tests that need a real child (its process group, its
timezone) start one and reach no network. The binding is a fake except where
a test is about the real one.

Owners are random UUIDs, every value is synthetic, and every role a test
makes is dropped after it. These tests need a Postgres server and skip
locally without one (`tests/pg_support.py`).
"""

# Pyright strict cannot resolve psycopg's row members, which is why test files
# that read rows carry these.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import io
import json
import logging
import os
import re
import signal
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest
from pg_support import open_owner_store
from test_hosted_auth import new_credentials_key_text
from test_hosted_requests import LoginRole, Server, copied_server

from harrier import connection
from harrier.connection import URL_VARIABLE, Connection, owner_claims
from harrier.credentials import CredentialUnreadable, parse_key, put_credential
from harrier.discovery import apify_allowed_now
from harrier.hostedschedule import claim_slot, finish_slot, load_hosted_jobs
from harrier.pgstore import postgres_connect
from harrier.schedule import ScheduleJob
from harrier.tracker.schema import SCHEDULE_READER_ROLE
from harrier_api import hostedscheduler
from harrier_api.hostedauth import RUN_DATABASE_URL_VARIABLE
from harrier_api.hostedscheduler import (
    CONCURRENCY_VARIABLE,
    CREDENTIALS_KEY_VARIABLE,
    TIMEOUT_VARIABLE,
    Process,
    Scheduler,
    read_settings,
    run_report,
    run_scheduler,
    spawn_group,
)
from harrier_api.hostedstore import HostedStartRefused, check_run_login_role, open_request_store
from harrier_api.runenv import BoundRun, ExtraVariableRefused, RunCredentialMissing, bind_run

SCHEDULER_LOGGER = "harrier_api.hostedscheduler"

# A Monday. In UTC the discovery slot is 09:00, and the academic one 09:30.
START = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
SLOT = "2026-10-12T09:00:00Z"
ACADEMIC_SLOT = "2026-10-12T09:30:00Z"


# --- the clock and the runs ---


class FakeClock:
    """Time that moves only when the scheduler sleeps."""

    def __init__(self, start: datetime = START) -> None:
        self.moment = start

    def now(self) -> datetime:
        return self.moment

    def monotonic(self) -> float:
        return (self.moment - EPOCH).total_seconds()

    def sleep(self, seconds: float) -> None:
        self.moment += timedelta(seconds=seconds)


class RealTimeClock(FakeClock):
    """The instant is fixed; waiting is real, for the tests that start a
    real child."""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class FakeProcess:
    """A run the test steers: it exits with `code` once `seconds` have
    passed on the clock, or never when `seconds` is None. Terminate and kill
    end it, as signals end a child, unless it ignores terminate."""

    def __init__(
        self,
        clock: FakeClock,
        seconds: float | None,
        code: int,
        *,
        ignores_terminate: bool,
        survives_kill: bool = False,
    ) -> None:
        self.clock = clock
        self.survives_kill = survives_kill
        # When terminate was called, on the clock's monotonic scale.
        self.terminated_at: float | None = None
        self.started = clock.monotonic()
        self.seconds = seconds
        self.code = code
        self.ignores_terminate = ignores_terminate
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        if self.killed and not self.survives_kill:
            return -9
        if self.terminated and not self.ignores_terminate:
            return -15
        if self.seconds is not None and self.clock.monotonic() - self.started >= self.seconds:
            return self.code
        return None

    def terminate(self) -> None:
        self.terminated = True
        if self.terminated_at is None:
            self.terminated_at = self.clock.monotonic()

    def kill(self) -> None:
        self.killed = True


@dataclass(frozen=True)
class Plan:
    """What a fake run does. The default exits 0 after five seconds."""

    seconds: float | None = 5
    code: int = 0
    ignores_terminate: bool = False
    # A process nothing can end, as one outside its group is: kill is sent
    # and it still runs.
    survives_kill: bool = False


HANGS = Plan(seconds=None)


@dataclass
class Spawned:
    owner: str
    argv: list[str]
    env: dict[str, str]
    cwd: Path
    at: datetime
    process: FakeProcess
    # How many fake runs were running, across owners and of this owner, the
    # moment this one started.
    running: int
    owner_running: int


@dataclass
class Fleet:
    """The fake binding and the fake spawn: what the scheduler starts runs
    through. A refusal is raised for its owner; a plan decides how its
    runs end."""

    clock: FakeClock
    root: Path
    plans: dict[str, Plan] = field(default_factory=lambda: dict[str, Plan]())
    refusals: dict[str, Exception] = field(default_factory=lambda: dict[str, Exception]())
    default: Plan = field(default_factory=Plan)
    bound: list[dict[str, Any]] = field(default_factory=lambda: list[dict[str, Any]]())
    spawned: list[Spawned] = field(default_factory=lambda: list[Spawned]())
    on_spawn: Callable[[Spawned], None] | None = None

    def bind(self, **kwargs: Any) -> BoundRun:
        self.bound.append(kwargs)
        owner = str(kwargs["owner"])
        if owner in self.refusals:
            raise self.refusals[owner]
        cwd = self.root / "owners" / owner
        cwd.mkdir(parents=True, exist_ok=True)
        # A real child must carry the store pin, as every child a test
        # starts must (tests/conftest.py); the fake binding names no store.
        env = {
            "OWNER": owner,
            "PATH": os.environ.get("PATH", ""),
            URL_VARIABLE: "",
            **kwargs["extra"],
        }
        return BoundRun(env=env, cwd=cwd, secrets=frozenset())

    def spawn(self, argv: Sequence[str], env: Mapping[str, str], cwd: Path) -> Process:
        owner = env["OWNER"]
        plan = self.plans.get(owner, self.default)
        process = FakeProcess(
            self.clock,
            plan.seconds,
            plan.code,
            ignores_terminate=plan.ignores_terminate,
            survives_kill=plan.survives_kill,
        )
        live = [s for s in self.spawned if s.process.poll() is None]
        entry = Spawned(
            owner,
            list(argv),
            dict(env),
            cwd,
            self.clock.now(),
            process,
            len(live),
            len([s for s in live if s.owner == owner]),
        )
        self.spawned.append(entry)
        if self.on_spawn is not None:
            self.on_spawn(entry)
        return process


# --- the rig: a store, a scheduler login, and owners ---

STATE_COLUMNS = (
    "owner_id::text",
    "job",
    "last_slot_at",
    "last_started_at",
    "last_finished_at",
    "last_outcome",
    "last_exit",
    "last_ok_at",
)


@dataclass
class Rig:
    server: Server
    scheduler: LoginRole
    run_login: LoginRole
    key_text: str
    tmp_path: Path
    clock: FakeClock
    fleet: Fleet
    jobs: dict[str, ScheduleJob]
    calls: list[str] = field(default_factory=lambda: list[str]())

    def environ(self, **overrides: str | None) -> dict[str, str]:
        """The scheduler machine's environment. A value of None removes
        the name."""
        env = {
            URL_VARIABLE: self.scheduler.url,
            RUN_DATABASE_URL_VARIABLE: self.run_login.url,
            CREDENTIALS_KEY_VARIABLE: self.key_text,
            CONCURRENCY_VARIABLE: "2",
            TIMEOUT_VARIABLE: "60",
            "PATH": os.environ.get("PATH", ""),
        }
        for name, value in overrides.items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        return env

    def run(self, environ: Mapping[str, str] | None = None, **kwargs: Any) -> int:
        options: dict[str, Any] = {
            "clock": self.clock,
            "spawn": self.fleet.spawn,
            "bind": self.fleet.bind,
            "jobs": self.jobs,
            "root": self.tmp_path,
        }
        return run_scheduler(
            environ if environ is not None else self.environ(), **{**options, **kwargs}
        )

    def sql(self, statement: str, params: Sequence[object] = ()) -> list[tuple[Any, ...]]:
        """As the migrating role, which bypasses the row policy."""
        with closing(postgres_connect(self.server.url)) as admin:
            cursor = admin.execute(statement.encode(), params)
            return cursor.fetchall() if cursor.description else []

    def owner(
        self,
        *,
        timezone: str = "UTC",
        jobs: Sequence[str] = ("discovery",),
        updated_at: str = "2026-01-01 00:00:00",
        apify: bool = False,
    ) -> str:
        """A synthetic owner who chose `jobs` in `timezone` long ago."""
        owner = self.server.owner()
        value = json.dumps({"timezone": timezone, "jobs": list(jobs)})
        self.sql(
            "INSERT INTO user_config (owner_id, kind, value, updated_at) "
            "VALUES (%s, 'schedule', %s, %s)",
            (owner, value, updated_at),
        )
        if apify:
            key = parse_key(self.key_text)
            assert key is not None
            with closing(open_owner_store(self.server.url, owner)) as store:
                put_credential(store, key, owner, "apify", {"token": f"synthetic-{uuid.uuid4()}"})
        return owner

    def put_state(self, owner: str, job: str, **columns: Any) -> None:
        values: dict[str, Any] = {
            "last_slot_at": SLOT,
            "last_started_at": SLOT,
            "last_finished_at": SLOT,
            "last_outcome": "ok",
            "last_exit": 0,
            "last_ok_at": SLOT,
            **columns,
        }
        names = ", ".join(("owner_id", "job", *values))
        marks = ", ".join(["%s"] * (len(values) + 2))
        self.sql(
            f"INSERT INTO schedule_state ({names}) VALUES ({marks})", (owner, job, *values.values())
        )

    def state(self) -> dict[tuple[str, str], dict[str, Any]]:
        rows = self.sql(f"SELECT {', '.join(STATE_COLUMNS)} FROM schedule_state")
        names = [column.split("::")[0] for column in STATE_COLUMNS]
        return {(row[0], row[1]): dict(zip(names, row, strict=True)) for row in rows}

    def outcomes(self) -> dict[str, str]:
        return {owner: row["last_outcome"] for (owner, _), row in self.state().items()}


@pytest.fixture(name="server")
def server_fixture(postgres_template: str) -> Iterator[Server]:
    yield from copied_server(postgres_template)


def scheduler_login(server: Server, *, reader: bool = True) -> LoginRole:
    login = server.login_role()
    if reader:
        server.sql(f"GRANT {SCHEDULE_READER_ROLE} TO {login.name}")
    return login


@pytest.fixture(name="rig")
def rig_fixture(server: Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    # Nothing a test starts may read this machine's environment or leave a
    # data directory in the checkout.
    monkeypatch.delenv("HARRIER_DATA_DIR", raising=False)
    clock = FakeClock()
    return Rig(
        server=server,
        scheduler=scheduler_login(server),
        run_login=server.login_role(),
        key_text=new_credentials_key_text(),
        tmp_path=tmp_path,
        clock=clock,
        fleet=Fleet(clock, tmp_path),
        jobs=load_hosted_jobs(Path(__file__).resolve().parents[3] / "config" / "schedule.json"),
    )


# --- one tick ---


def test_one_scheduler_start_runs_one_tick_and_exits(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One start, with due slots for two owners and a fake binding, starts
    both runs, waits for both, records both outcomes and exits 0; with
    nothing due it lists once and exits 0 (spec 108, Hosting budget,
    "What one start does")."""
    listings: list[int] = []
    original = hostedscheduler.list_inputs

    def counted(conn: Connection) -> Any:
        listings.append(1)
        return original(conn)

    monkeypatch.setattr(hostedscheduler, "list_inputs", counted)
    first, second = rig.owner(), rig.owner()

    assert rig.run() == 0

    assert listings == [1]
    assert sorted(s.owner for s in rig.fleet.spawned) == sorted([first, second])
    state = rig.state()
    assert set(state) == {(first, "discovery"), (second, "discovery")}
    for row in state.values():
        assert (row["last_slot_at"], row["last_outcome"], row["last_exit"]) == (SLOT, "ok", 0)
        assert row["last_started_at"] and row["last_finished_at"]
        assert row["last_ok_at"] == row["last_finished_at"]
    # Every run was a scheduled discovery, started as the committed command.
    assert all(s.argv[1:3] == ["-m", "harrier_cli.main"] for s in rig.fleet.spawned)
    assert all(s.argv[3:] == ["discover", "--scheduled"] for s in rig.fleet.spawned)

    # The same slots are claimed, so a second start finds nothing due: it
    # lists once, starts nothing and exits 0.
    listings.clear()
    rig.fleet.spawned.clear()
    assert rig.run() == 0
    assert (listings, rig.fleet.spawned) == ([1], [])


def test_a_choice_in_the_future_or_no_owner_lists_and_exits(rig: Rig) -> None:
    """With nobody scheduled, a tick lists, starts nothing and exits 0."""
    assert rig.run() == 0
    assert rig.fleet.spawned == []
    rig.owner(updated_at="2026-10-12 09:30:00")
    assert rig.run() == 0
    assert rig.fleet.spawned == []


def test_an_outage_starts_each_jobs_latest_slot_once(rig: Rig) -> None:
    """After days of no scheduler, one start runs each job once, at its
    latest slot (spec 108, "The slot")."""
    owner = rig.owner(jobs=("discovery", "academic-discovery"), apify=True)
    stale = "2026-10-05T20:00:00Z"
    rig.put_state(owner, "discovery", last_slot_at=stale)
    rig.put_state(owner, "academic-discovery", last_slot_at="2026-09-28T09:30:00Z")

    assert rig.run() == 0

    assert [s.argv[3:] for s in rig.fleet.spawned] == [
        ["discover", "--scheduled"],
        ["discover", "--scheduled", "--configured-tracks"],
    ]
    state = rig.state()
    assert state[(owner, "discovery")]["last_slot_at"] == SLOT
    assert state[(owner, "academic-discovery")]["last_slot_at"] == ACADEMIC_SLOT


# --- the claim ---


def claimed(conn: Connection, job: str, slot: str, **kwargs: Any) -> bool:
    return claim_slot(conn, job, slot, started_at="2026-10-12T10:00:00Z", **kwargs)


def blocked_backends(rig: Rig) -> int:
    """How many backends of this test's database wait on a lock. Read as the
    server's superuser: another role's wait is hidden from a role that is
    neither a superuser nor a member of pg_read_all_stats."""
    with rig.server.superuser() as admin:
        row = admin.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND wait_event_type = 'Lock'"
        ).fetchone()
    assert row is not None
    return int(row[0])


def race(rig: Rig, owner: str, slot: str) -> tuple[bool, bool]:
    """Two connections, both as `owner`, claim `slot`. The first claims and
    holds its transaction open; the second starts and is made to wait on it;
    the first commits. Returns what each claim answered."""
    first_claimed = threading.Event()
    let_first_commit = threading.Event()
    answers: dict[str, bool] = {}
    failures: list[BaseException] = []

    def first() -> None:
        try:
            with closing(postgres_connect(rig.scheduler.url)) as driver:
                conn = Connection.postgres(driver, claims=owner_claims(owner))
                with conn.transaction():
                    answers["first"] = claimed(conn, "discovery", slot)
                    first_claimed.set()
                    assert let_first_commit.wait(20)
        except BaseException as error:
            failures.append(error)
            first_claimed.set()

    def second() -> None:
        try:
            with closing(postgres_connect(rig.scheduler.url)) as driver:
                conn = Connection.postgres(driver, claims=owner_claims(owner))
                with conn.transaction():
                    answers["second"] = claimed(conn, "discovery", slot)
        except BaseException as error:
            failures.append(error)

    one = threading.Thread(target=first)
    one.start()
    assert first_claimed.wait(20)
    two = threading.Thread(target=second)
    two.start()
    # The second claim must be waiting on the first before it commits, or
    # the two did not race.
    deadline = time.monotonic() + 20
    while blocked_backends(rig) == 0 and two.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    waited = blocked_backends(rig) > 0
    let_first_commit.set()
    one.join(30)
    two.join(30)
    assert failures == []
    assert waited, "the second claim never waited on the first"
    return answers["first"], answers["second"]


def test_a_slot_starts_once(rig: Rig) -> None:
    """Claiming one slot twice, from two connections as the same owner,
    starts it once, whether the row is new or an older slot's; a finish for
    an older slot leaves a newer claim unchanged (spec 108, "Claiming and
    starting a run as its owner")."""
    owner = rig.server.owner()
    other = rig.server.owner()

    # The first claim of a job inserts its row: the second meets the unique
    # key and loses.
    assert race(rig, owner, SLOT) == (True, False)
    [row] = [r for (o, _), r in rig.state().items() if o == owner]
    assert (row["last_slot_at"], row["last_outcome"]) == (SLOT, "running")

    # A later slot updates the row: the second finds the row already moved
    # and loses.
    later = "2026-10-12T13:00:00Z"
    assert race(rig, owner, later) == (True, False)
    assert rig.state()[(owner, "discovery")]["last_slot_at"] == later

    # Another owner's claim of the same job and slot is theirs, not blocked
    # by this one.
    with closing(postgres_connect(rig.scheduler.url)) as driver:
        theirs = Connection.postgres(driver, claims=owner_claims(other))
        assert claimed(theirs, "discovery", SLOT) is True
        # The slot is claimed already, or older than the claim: not theirs.
        assert claimed(theirs, "discovery", SLOT) is False
        mine = Connection.postgres(driver, claims=owner_claims(owner))
        assert claimed(mine, "discovery", SLOT) is False
        assert claimed(mine, "discovery", later) is False

        # A finish for the older slot changes nothing of the newer claim.
        stale = finish_slot(
            mine,
            "discovery",
            SLOT,
            outcome="ok",
            exit_code=0,
            finished_at="2026-10-12T11:00:00Z",
            ok_at="2026-10-12T11:00:00Z",
        )
        assert stale is False
        row = rig.state()[(owner, "discovery")]
        assert (row["last_slot_at"], row["last_outcome"], row["last_ok_at"]) == (
            later,
            "running",
            None,
        )
        # A finish for the claimed slot is recorded.
        assert finish_slot(
            mine,
            "discovery",
            later,
            outcome="failed",
            exit_code=3,
            finished_at="2026-10-12T14:00:00Z",
        )
        row = rig.state()[(owner, "discovery")]
        assert (row["last_outcome"], row["last_exit"], row["last_ok_at"]) == ("failed", 3, None)


def test_a_missed_claim_is_recorded_without_a_start(rig: Rig) -> None:
    owner = rig.server.owner()
    with closing(postgres_connect(rig.scheduler.url)) as driver:
        conn = Connection.postgres(driver, claims=owner_claims(owner))
        assert claimed(conn, "discovery", SLOT, outcome="missed") is True
        # A later slot claims over it, as running.
        assert claimed(conn, "discovery", "2026-10-12T13:00:00Z") is True
    row = rig.state()[(owner, "discovery")]
    assert row["last_slot_at"] == "2026-10-12T13:00:00Z"
    assert (row["last_outcome"], row["last_finished_at"]) == ("running", None)
    # A missed claim has no start, and is finished when it is claimed.
    other = rig.server.owner()
    with closing(postgres_connect(rig.scheduler.url)) as driver:
        conn = Connection.postgres(driver, claims=owner_claims(other))
        assert claimed(conn, "discovery", SLOT, outcome="missed") is True
    row = rig.state()[(other, "discovery")]
    assert (row["last_outcome"], row["last_started_at"], row["last_exit"]) == ("missed", None, None)
    assert row["last_finished_at"] == "2026-10-12T10:00:00Z"


# --- isolation between owners ---


def test_one_owner_failing_does_not_stop_another(rig: Rig) -> None:
    """Four owners whose runs exit 3, exit 1, hang past the timeout and exit
    0: the fourth owner's run starts and finishes ok while the others are
    recorded failed, crashed and timed_out, and the hung child is stopped
    (spec 108, "Isolation")."""
    owners = sorted(rig.owner() for _ in range(4))
    exits_3, exits_1, hangs, exits_0 = owners
    rig.fleet.plans = {
        exits_3: Plan(seconds=3, code=3),
        exits_1: Plan(seconds=3, code=1),
        hangs: HANGS,
        exits_0: Plan(seconds=10, code=0),
    }

    assert rig.run() == 0

    assert rig.outcomes() == {
        exits_3: "failed",
        exits_1: "crashed",
        hangs: "timed_out",
        exits_0: "ok",
    }
    state = {owner: row for (owner, _), row in rig.state().items()}
    assert (state[exits_3]["last_exit"], state[exits_1]["last_exit"]) == (3, 1)
    assert (state[hangs]["last_exit"], state[exits_0]["last_exit"]) == (None, 0)
    assert state[exits_0]["last_ok_at"] and not state[hangs]["last_ok_at"]
    by_owner = {s.owner: s for s in rig.fleet.spawned}
    # The hung child was terminated and is gone; the others were left alone.
    assert by_owner[hangs].process.terminated and by_owner[hangs].process.poll() is not None
    assert not by_owner[exits_0].process.terminated
    # The fourth owner started while the third was still hung.
    assert by_owner[exits_0].running >= 1


def test_a_run_that_ignores_terminate_is_killed(rig: Rig) -> None:
    owner = rig.owner()
    rig.fleet.plans = {owner: Plan(seconds=None, ignores_terminate=True)}
    assert rig.run() == 0
    [spawned] = rig.fleet.spawned
    assert spawned.process.terminated and spawned.process.killed
    assert rig.outcomes() == {owner: "timed_out"}
    # Terminated at the timeout, killed after the grace.
    waited = (rig.clock.now() - START).total_seconds()
    assert 60 + hostedscheduler.TERMINATE_GRACE_SECONDS <= waited < 60 + 3 * 6


def test_a_run_that_finishes_at_the_timeout_keeps_its_exit(rig: Rig) -> None:
    owner = rig.owner()
    rig.fleet.plans = {owner: Plan(seconds=60, code=3)}
    assert rig.run() == 0
    assert rig.outcomes() == {owner: "failed"}


def test_a_timed_out_run_is_stopped_with_its_group(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real child that starts a grandchild: the timeout stops both, so
    remote work started by a grandchild stops charging (spec 108, "Finishing
    a run"). The child leads a process group of its own."""
    monkeypatch.setattr(hostedscheduler, "POLL_SECONDS", 0.05)
    script = (
        "import subprocess, sys, time\n"
        "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open(sys.argv[1], 'w').write(str(grandchild.pid))\n"
        "time.sleep(60)\n"
    )
    pid_file = rig.tmp_path / "grandchild.pid"

    def spawn(argv: Sequence[str], env: Mapping[str, str], cwd: Path) -> Process:
        return spawn_group([sys.executable, "-c", script, str(pid_file)], env, cwd)

    owner = rig.owner()
    grandchild = 0
    try:
        code = rig.run(rig.environ(**{TIMEOUT_VARIABLE: "2"}), clock=RealTimeClock(), spawn=spawn)
        assert code == 0
        grandchild = int(pid_file.read_text())
        assert rig.outcomes() == {owner: "timed_out"}
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail("the grandchild outlived its group")
    finally:
        if grandchild:
            with suppress(ProcessLookupError):
                os.kill(grandchild, 9)


# --- refusals ---


def test_a_refused_run_is_recorded(rig: Rig) -> None:
    """A binding refusal for a missing credential is `not_configured`; any
    other refusal is `refused`. Nothing starts for either (spec 108,
    "Finishing a run")."""
    missing, unreadable, extra, other, fine = (rig.owner() for _ in range(5))
    rig.fleet.refusals = {
        missing: RunCredentialMissing("apify"),
        unreadable: CredentialUnreadable("apify", "old-key"),
        extra: ExtraVariableRefused("a run's environment does not take PATH"),
        other: RuntimeError("anything else"),
    }

    assert rig.run() == 0

    assert rig.outcomes() == {
        missing: "not_configured",
        unreadable: "refused",
        extra: "refused",
        other: "refused",
        fine: "ok",
    }
    assert [s.owner for s in rig.fleet.spawned] == [fine]
    state = {owner: row for (owner, _), row in rig.state().items()}
    for owner in (missing, unreadable, extra, other):
        # Never started, never ok, and a finish time: it was decided.
        assert state[owner]["last_exit"] is None and state[owner]["last_ok_at"] is None
        assert state[owner]["last_finished_at"]


def test_the_binding_gets_the_owners_timezone_and_the_jobs_needs(rig: Rig) -> None:
    """The scheduler adds only `TZ` to the binding, and asks it for the
    credentials the job needs (spec 108, "Claiming and starting a run as its
    owner")."""
    owner = rig.owner(timezone="Asia/Tokyo", jobs=("discovery", "academic-discovery"), apify=True)
    rig.put_state(owner, "discovery", last_slot_at="2026-10-11T20:00:00Z")
    rig.put_state(owner, "academic-discovery", last_slot_at="2026-10-05T09:30:00Z")
    rig.clock.moment = datetime(2026, 10, 12, 3, 0, tzinfo=UTC)  # 12:00 in Tokyo

    assert rig.run() == 0

    by_job = {b["needs"]: b for b in rig.fleet.bound}
    assert set(by_job) == {(), ("apify",)}
    assert all(b["extra"] == {"TZ": "Asia/Tokyo"} for b in rig.fleet.bound)
    assert all(b["owner"] == owner for b in rig.fleet.bound)
    # The credentials it binds are the owner's, read as the owner.
    assert [c.kind for c in by_job[("apify",)]["stored"]] == ["apify"]


def test_the_real_binding_refuses_a_job_whose_credential_is_unset(rig: Rig) -> None:
    """Through spec 106's real binding: academic discovery without an Apify
    credential is `not_configured` and nothing starts, while discovery,
    which needs none, starts. With the credential, both start, carrying the
    owner's token and `TZ`."""
    bare = rig.owner(timezone="Europe/Amsterdam", jobs=("discovery", "academic-discovery"))
    equipped = rig.owner(jobs=("academic-discovery",), apify=True)
    rig.clock.moment = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)

    def capture(argv: Sequence[str], env: Mapping[str, str], cwd: Path) -> Process:
        return rig.fleet.spawn(argv, {"OWNER": env["HARRIER_RUN_OWNER"], **env}, cwd)

    assert rig.run(bind=bind_run, spawn=capture) == 0

    state = rig.state()
    assert state[(bare, "academic-discovery")]["last_outcome"] == "not_configured"
    assert state[(bare, "discovery")]["last_outcome"] == "ok"
    assert state[(equipped, "academic-discovery")]["last_outcome"] == "ok"
    started = {(s.owner, s.argv[-1]) for s in rig.fleet.spawned}
    assert started == {(bare, "--scheduled"), (equipped, "--configured-tracks")}
    env = {s.owner: s.env for s in rig.fleet.spawned}
    assert env[bare]["TZ"] == "Europe/Amsterdam" and "APIFY_TOKEN" not in env[bare]
    assert env[equipped]["APIFY_TOKEN"].startswith("synthetic-") and env[equipped]["TZ"] == "UTC"
    # The owner's run directory is the child's working directory.
    assert {s.owner: s.cwd for s in rig.fleet.spawned} == {
        owner: rig.tmp_path / "owners" / owner for owner in (bare, equipped)
    }


# --- one run per owner, and the queue ---


def test_one_owner_runs_one_scheduled_run_at_a_time(rig: Rig) -> None:
    """With room to run both, an owner's two jobs run one after the other,
    and a slot that waited past the next slot is recorded `missed` and its
    owner's newer slot runs instead (spec 108, "Isolation", Failure modes)."""
    # Two jobs of one owner, concurrency 3: never two at once.
    solo = rig.owner(jobs=("discovery", "academic-discovery"), apify=True)
    rig.fleet.default = Plan(seconds=30)
    assert rig.run(rig.environ(**{CONCURRENCY_VARIABLE: "3"})) == 0
    first, second = rig.fleet.spawned
    assert (first.owner_running, second.owner_running) == (0, 0)
    assert second.at >= first.at + timedelta(seconds=30)
    assert [s.argv[-1] for s in rig.fleet.spawned] == ["--scheduled", "--configured-tracks"]
    assert set(rig.outcomes()) == {solo}

    # Three owners and room for one run. The first holds the machine from
    # 12:30 to 13:30, past the 13:00 slot, so each owner still waiting has
    # its 09:00 slot recorded `missed` and runs the 13:00 slot.
    rig.sql("DELETE FROM schedule_state")
    rig.sql("DELETE FROM user_config WHERE kind = 'schedule'")
    rig.fleet.spawned.clear()
    rig.clock.moment = datetime(2026, 10, 12, 12, 30, tzinfo=UTC)
    a, b, c = sorted(rig.owner() for _ in range(3))
    rig.fleet.plans = {a: Plan(seconds=3600)}
    seen: list[dict[str, tuple[str, str]]] = []

    def watch(spawned: Spawned) -> None:
        seen.append(
            {
                owner: (row["last_slot_at"], row["last_outcome"])
                for (owner, _), row in rig.state().items()
            }
        )

    rig.fleet.on_spawn = watch
    environ = rig.environ(**{CONCURRENCY_VARIABLE: "1", TIMEOUT_VARIABLE: "86400"})
    assert rig.run(environ) == 0

    assert [s.owner for s in rig.fleet.spawned] == [a, b, c]
    assert rig.fleet.spawned[1].at >= datetime(2026, 10, 12, 13, 30, tzinfo=UTC)
    # When b's 13:00 run starts, c is still waiting with its 09:00 slot
    # recorded missed, and a has finished its 09:00 slot.
    assert seen[1][c] == (SLOT, "missed")
    assert seen[1][a] == (SLOT, "ok")
    assert seen[1][b] == ("2026-10-12T13:00:00Z", "running")
    final = {
        owner: (row["last_slot_at"], row["last_outcome"]) for (owner, _), row in rig.state().items()
    }
    assert final == {
        a: (SLOT, "ok"),
        b: ("2026-10-12T13:00:00Z", "ok"),
        c: ("2026-10-12T13:00:00Z", "ok"),
    }


def test_the_queue_starts_never_started_owners_first(rig: Rig) -> None:
    """With one run at a time, the order runs are started in is the queue's:
    never-started before started, then the earlier last start, then owner
    id (spec 108, "A tick", step 3)."""
    low, middle, high = sorted(rig.owner() for _ in range(3))
    rig.put_state(
        low,
        "discovery",
        last_slot_at="2026-10-12T07:00:00Z",
        last_started_at="2026-10-12T07:01:00Z",
    )
    rig.put_state(
        middle,
        "discovery",
        last_slot_at="2026-10-12T07:00:00Z",
        last_started_at="2026-10-12T06:00:00Z",
    )
    assert rig.run(rig.environ(**{CONCURRENCY_VARIABLE: "1"})) == 0
    assert [s.owner for s in rig.fleet.spawned] == [high, middle, low]


# --- the lock ---


def hold_the_lock(rig: Rig) -> psycopg.Connection[Any]:
    """Another session takes the scheduler's lock."""
    holder = psycopg.connect(rig.server.url, autocommit=True)
    row = holder.execute(
        "SELECT pg_advisory_lock(hashtext('harrier.scheduler'), 0), pg_backend_pid()"
    ).fetchone()
    assert row is not None
    return holder


def test_a_second_scheduler_stands_by(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """With the lock held by another session, a start lists nothing, starts
    nothing and exits 0, saying so once. On acquiring it, `running` rows
    become `interrupted` and their slots are not started again; the next
    slot is (spec 108, "One scheduler at a time", "Finishing a run")."""
    caplog.set_level(logging.INFO, logger=SCHEDULER_LOGGER)
    listed: list[int] = []
    original = hostedscheduler.list_inputs

    def counted(conn: Connection) -> Any:
        listed.append(1)
        return original(conn)

    monkeypatch.setattr(hostedscheduler, "list_inputs", counted)
    owner = rig.owner(jobs=("discovery", "academic-discovery"), apify=True)
    fresh = rig.owner()
    # A run its scheduler never finished, and a slot that is due.
    rig.put_state(owner, "discovery", last_outcome="running", last_exit=None, last_finished_at=None)
    rig.put_state(owner, "academic-discovery", last_slot_at="2026-10-05T09:30:00Z")

    holder = hold_the_lock(rig)
    try:
        assert rig.run() == 0
    finally:
        holder.close()
    assert listed == [] and rig.fleet.spawned == []
    assert rig.state()[(owner, "discovery")]["last_outcome"] == "running"
    standing = [r for r in caplog.records if "standing by" in r.getMessage()]
    assert [r.getMessage() for r in standing] == [
        "scheduler standing by: another instance holds the lock"
    ]

    # The lock is free: this start holds it and sweeps. The owner's
    # discovery slot is not started again, and their academic slot, which
    # was blocked while discovery said running, and a new owner's slot run.
    assert rig.run() == 0
    swept = rig.state()[(owner, "discovery")]
    assert (swept["last_slot_at"], swept["last_outcome"], swept["last_exit"]) == (
        SLOT,
        "interrupted",
        None,
    )
    assert swept["last_finished_at"] and swept["last_ok_at"] == SLOT
    assert sorted((s.owner, s.argv[-1]) for s in rig.fleet.spawned) == sorted(
        [(owner, "--configured-tracks"), (fresh, "--scheduled")]
    )

    # The next slot is started.
    rig.fleet.spawned.clear()
    rig.clock.moment = datetime(2026, 10, 12, 13, 5, tzinfo=UTC)
    assert rig.run() == 0
    assert sorted(s.owner for s in rig.fleet.spawned) == sorted([owner, fresh])
    assert rig.state()[(owner, "discovery")]["last_slot_at"] == "2026-10-12T13:00:00Z"


def lock_pids(rig: Rig) -> list[int]:
    return [
        int(row[0])
        for row in rig.sql(
            "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND objsubid = 2 AND objid = 0 "
            "AND classid::bigint = (hashtext('harrier.scheduler')::bigint & 4294967295) "
            "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
        )
    ]


def test_a_step_that_fails_for_one_owner_leaves_the_others(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Every per-owner step catches every exception, logs it with the owner's
    id and the exception's class, and goes on with the next owner (spec 108,
    "Isolation"): a failed claim starts nothing and writes nothing, so the
    next start tries the slot again; a failed spawn is recorded `crashed`;
    a failed sweep leaves the owner's run in progress, so none of their slots
    starts."""
    caplog.set_level(logging.INFO, logger=SCHEDULER_LOGGER)
    claim_fails, spawn_fails, sweep_fails, fine = sorted(rig.owner() for _ in range(4))
    rig.put_state(
        sweep_fails,
        "discovery",
        last_slot_at="2026-10-11T20:00:00Z",
        last_outcome="running",
        last_exit=None,
    )
    refused = connection.StoreDatabaseError("the server refused the statement")

    original_claim = hostedscheduler.claim_slot
    claims: list[str] = []

    def claim(conn: Connection, job: str, slot: str, **kwargs: Any) -> bool:
        claims.append(job)
        if len(claims) == 1:
            raise refused
        return original_claim(conn, job, slot, **kwargs)

    def sweep(conn: Connection, **kwargs: Any) -> int:
        raise refused

    def spawn(argv: Sequence[str], env: Mapping[str, str], cwd: Path) -> Process:
        if env["OWNER"] == spawn_fails:
            raise OSError("no such interpreter")
        return rig.fleet.spawn(argv, env, cwd)

    monkeypatch.setattr(hostedscheduler, "claim_slot", claim)
    monkeypatch.setattr(hostedscheduler, "sweep_running", sweep)

    assert rig.run(spawn=spawn) == 0

    state = {owner: row for (owner, _), row in rig.state().items()}
    # Nothing was claimed for the first, so it has no row and starts nothing.
    assert claim_fails not in state
    assert state[spawn_fails]["last_outcome"] == "crashed"
    assert state[spawn_fails]["last_exit"] is None
    assert state[fine]["last_outcome"] == "ok"
    # The sweep failed, so the run it could not mark is still in progress.
    assert state[sweep_fails]["last_outcome"] == "running"
    assert [s.owner for s in rig.fleet.spawned] == [fine]
    failures = {
        (m.split("owner=")[1].split()[0], m.split("step=")[1].split()[0])
        for m in (r.getMessage() for r in caplog.records)
        if m.startswith("scheduled step failed")
    }
    assert failures == {
        (claim_fails, "claim"),
        (spawn_fails, "spawn"),
        (sweep_fails, "sweep"),
    }
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "StoreDatabaseError: the server refused the statement" in logged
    assert "no such interpreter" not in logged and "OSError" in logged


def test_an_outcome_that_cannot_be_recorded_is_not_a_clean_tick(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run ended and the store refused the outcome: the tick exits 1, and
    the row stays `running` for the next start to mark `interrupted`."""
    owner = rig.owner()

    def finish(conn: Connection, *args: Any, **kwargs: Any) -> bool:
        raise connection.StoreDatabaseError("the server refused the statement")

    monkeypatch.setattr(hostedscheduler, "finish_slot", finish)
    assert rig.run() == 1
    assert rig.outcomes() == {owner: "running"}


def test_a_listing_that_fails_ends_the_tick_and_starts_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=SCHEDULER_LOGGER)
    rig.owner()

    def listing(conn: Connection) -> Any:
        raise connection.StoreDatabaseError("the server refused the statement")

    monkeypatch.setattr(hostedscheduler, "list_inputs", listing)
    assert rig.run() == 1
    assert rig.fleet.spawned == [] and rig.state() == {}
    assert "scheduler could not list schedules: StoreDatabaseError" in caplog.text


def test_a_lost_lock_stops_the_tick_and_keeps_true_outcomes(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """When the lock connection fails, the scheduler terminates its
    children, records each `interrupted`, logs it, and exits 1. A child
    that finished first keeps its true outcome (spec 108, "One scheduler at
    a time")."""
    caplog.set_level(logging.INFO, logger=SCHEDULER_LOGGER)
    finishes, hangs = sorted(rig.owner() for _ in range(2))
    # The first child exits at the very second the lock check runs, so its
    # exit is first seen inside the abandon, before it is reaped. The second
    # ignores terminate, so it must be killed.
    rig.fleet.plans = {
        finishes: Plan(seconds=hostedscheduler.LOCK_CHECK_SECONDS, code=3),
        hangs: Plan(seconds=None, ignores_terminate=True),
    }

    def lose_the_lock(spawned: Spawned) -> None:
        if len(rig.fleet.spawned) == 2:
            with rig.server.superuser() as admin:
                for pid in lock_pids(rig):
                    admin.execute("SELECT pg_terminate_backend(%s)", (pid,))

    rig.fleet.on_spawn = lose_the_lock
    assert rig.run() == 1

    assert rig.outcomes() == {finishes: "failed", hangs: "interrupted"}
    first, second = rig.fleet.spawned[0].process, rig.fleet.spawned[1].process
    assert not first.terminated and not first.killed
    assert second.terminated and second.killed
    assert "scheduler lost its lock" in [r.getMessage() for r in caplog.records]
    # The lock is not held by anyone after the tick.
    assert lock_pids(rig) == []


def test_a_stop_signal_ends_the_tick_like_a_lost_lock(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """Fly stops a tick for an update with SIGINT then SIGTERM. The
    scheduler terminates its children and records them `interrupted`, so no
    row waits for the next start to be swept (spec 108, Amendment
    (2026-10-12))."""
    caplog.set_level(logging.INFO, logger=SCHEDULER_LOGGER)
    owner = rig.owner()
    stop = threading.Event()
    rig.fleet.plans = {owner: HANGS}
    rig.fleet.on_spawn = lambda _spawned: stop.set()

    assert rig.run(stop=stop) == 1

    assert rig.outcomes() == {owner: "interrupted"}
    assert rig.fleet.spawned[0].process.terminated
    assert "scheduler stopped" in [r.getMessage() for r in caplog.records]


def test_a_connection_that_is_not_one_session_fails_the_start(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A session advisory lock does not survive a transaction pooler. A lock
    this session cannot be seen to hold fails the start rather than running
    without a lock it cannot keep (spec 108, Failure modes)."""
    rig.owner()

    def not_held(_lock: hostedscheduler.AdvisoryLock) -> bool:
        return False

    monkeypatch.setattr(hostedscheduler.AdvisoryLock, "held", not_held)
    assert rig.run() == 1
    assert "session advisory lock" in capsys.readouterr().err
    assert rig.fleet.spawned == []


# --- who the scheduler acts as ---


def test_the_scheduler_acts_as_each_owner(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every statement the tick runs outside the listing runs with
    `current_user` `harrier_tenant` and one owner's claims; the listing
    transaction runs as the reader and sets no claims. No statement names a
    tenant column (spec 108, "A tick")."""
    backend = connection._PostgresBackend  # pyright: ignore[reportPrivateUsage]
    original = backend.run
    seen: list[tuple[str, str, str | None]] = []

    def probing(self: Any, sql: str, statement: Any, params: Any, *, in_block: bool) -> Any:
        row = self.driver.execute(
            b"SELECT current_user::text, "
            b"nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub'"
        ).fetchone()
        seen.append((sql, row[0], row[1]))
        return original(self, sql, statement, params, in_block=in_block)

    first = rig.owner(jobs=("discovery", "academic-discovery"), apify=True)
    second = rig.owner()
    rig.fleet.refusals = {}

    class ProbingDriver:
        """The scheduler's connection for the listing, which goes past the seam
        (the seam's `execute()` refuses the reader's `SET LOCAL ROLE`): the role
        and claims in force are read just before the listing runs."""

        def __init__(self, driver: Any) -> None:
            self.driver = driver

        def transaction(self, **kwargs: Any) -> Any:
            return self.driver.transaction(**kwargs)

        def execute(self, query: bytes) -> Any:
            if b"harrier_schedule_inputs" in query:
                row = self.driver.execute(
                    b"SELECT current_user::text, "
                    b"nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub'"
                ).fetchone()
                listed.append((row[0], row[1]))
            return self.driver.execute(query)

    listed: list[tuple[str, str | None]] = []
    original_listing = hostedscheduler.list_inputs

    def probed_listing(driver: Any) -> Any:
        return original_listing(ProbingDriver(driver))

    with monkeypatch.context() as patched:
        patched.setattr(backend, "run", probing)
        patched.setattr(hostedscheduler, "list_inputs", probed_listing)
        scheduler = Scheduler(
            connect=lambda: postgres_connect(rig.scheduler.url),
            jobs=rig.jobs,
            settings=read_settings(rig.environ()),
            server_env=rig.environ(),
            root=rig.tmp_path,
            clock=rig.clock,
            spawn=rig.fleet.spawn,
            bind=rig.fleet.bind,
        )
        assert scheduler.tick() == 0

    # The listing ran once, as the reader, with no claims.
    assert listed == [(SCHEDULE_READER_ROLE, None)]
    # Every other statement went through the seam, as an owner, and none of
    # them is a session statement the seam now refuses.
    others = list(seen)
    assert others, "the tick ran no statement as an owner"
    assert not any(sql.lstrip().upper().startswith(("SET ", "RESET ")) for sql, _, _ in seen)
    assert {(role, owner) for _, role, owner in others} == {
        ("harrier_tenant", first),
        ("harrier_tenant", second),
    }
    assert not any("owner_id" in sql for sql, _, _ in seen)
    # Each owner claimed, read their credentials and finished, and the
    # credentials were read as the owner they belong to.
    reads = {owner for sql, _, owner in others if "FROM credentials" in sql}
    assert reads == {first, second}


def test_no_connection_waits_open_while_runs_run(rig: Rig) -> None:
    """The scheduler holds its lock connection and no other while its runs
    run: a connection held through a run of an hour is one a pooler or a
    firewall may have dropped when the outcome is written (spec 108,
    "Finishing a run")."""
    rig.owner()
    rig.owner()
    open_sessions: list[int] = []

    def count(_spawned: Spawned) -> None:
        with rig.server.superuser() as admin:
            row = admin.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE usename = %s", (rig.scheduler.name,)
            ).fetchone()
        assert row is not None
        open_sessions.append(int(row[0]))

    rig.fleet.on_spawn = count
    assert rig.run() == 0
    assert open_sessions == [1, 1]
    assert set(rig.outcomes().values()) == {"ok"}


# --- what the scheduler refuses to start with ---


def refuse_superuser(rig: Rig) -> dict[str, str | None]:
    return {URL_VARIABLE: scheduler_login_with(rig, "SUPERUSER").url}


def scheduler_login_with(rig: Rig, attributes: str) -> LoginRole:
    login = rig.server.login_role(attributes)
    rig.server.sql(f"GRANT {SCHEDULE_READER_ROLE} TO {login.name}")
    return login


def refuse_bypassrls(rig: Rig) -> dict[str, str | None]:
    return {URL_VARIABLE: scheduler_login_with(rig, "BYPASSRLS").url}


def refuse_service_role_member(rig: Rig) -> dict[str, str | None]:
    login = scheduler_login_with(rig, "")
    rig.server.sql(f"GRANT service_role TO {login.name}")
    return {URL_VARIABLE: login.url}


def refuse_postgres_member(rig: Rig) -> dict[str, str | None]:
    login = scheduler_login_with(rig, "")
    rig.server.sql(f"GRANT postgres TO {login.name}")
    return {URL_VARIABLE: login.url}


def refuse_table_owner(rig: Rig) -> dict[str, str | None]:
    login = scheduler_login_with(rig, "")
    rig.server.sql(
        "CREATE TABLE public.harrier_owned_probe (x integer)",
        f"ALTER TABLE public.harrier_owned_probe OWNER TO {login.name}",
    )
    return {URL_VARIABLE: login.url}


def refuse_no_tenant(rig: Rig) -> dict[str, str | None]:
    login = rig.server.login_role(tenant="none")
    rig.server.sql(f"GRANT {SCHEDULE_READER_ROLE} TO {login.name}")
    return {URL_VARIABLE: login.url}


def refuse_no_reader(rig: Rig) -> dict[str, str | None]:
    return {URL_VARIABLE: scheduler_login(rig.server, reader=False).url}


def refuse_function_owner(rig: Rig) -> dict[str, str | None]:
    owner = rig.server.role("NOLOGIN")
    rig.server.sql(f"ALTER FUNCTION public.harrier_schedule_inputs() OWNER TO {owner}")
    return {}


def refuse_old_store(rig: Rig) -> dict[str, str | None]:
    rig.sql("DELETE FROM schema_version WHERE version = (SELECT max(version) FROM schema_version)")
    return {}


def refuse_no_function(rig: Rig) -> dict[str, str | None]:
    rig.server.sql("DROP FUNCTION public.harrier_schedule_inputs()")
    return {}


def refuse_skipped_version(rig: Rig) -> dict[str, str | None]:
    # A declared migration below the newest, never recorded: the runner skips
    # it for ever, so the store is refused (spec 103).
    rig.sql("DELETE FROM schema_version WHERE version = 12")
    return {}


def refuse_reader_without_set(rig: Rig) -> dict[str, str | None]:
    # Postgres 16 split the right to set a role from membership.
    if int(rig.sql("SHOW server_version_num")[0][0]) < 160000:
        pytest.skip("GRANT ... WITH SET FALSE needs Postgres 16")
    login = scheduler_login(rig.server, reader=False)
    rig.server.sql(f"GRANT {SCHEDULE_READER_ROLE} TO {login.name} WITH SET FALSE")
    return {URL_VARIABLE: login.url}


def refuse_run_login_with_the_reader(rig: Rig) -> dict[str, str | None]:
    return {RUN_DATABASE_URL_VARIABLE: scheduler_login(rig.server).url}


REFUSALS: tuple[tuple[str, Callable[[Rig], dict[str, str | None]], str], ...] = (
    ("a superuser login", refuse_superuser, "superuser"),
    ("a login that bypasses row security", refuse_bypassrls, "bypassrls"),
    ("a member of service_role", refuse_service_role_member, "service_role"),
    ("a member of postgres", refuse_postgres_member, "postgres"),
    ("a login that owns a table in public", refuse_table_owner, "owns a table in public"),
    ("a login that cannot become the tenant", refuse_no_tenant, "cannot set role harrier_tenant"),
    (
        "a login that cannot become the reader",
        refuse_no_reader,
        "cannot set role harrier_schedule_reader",
    ),
    (
        "a function whose owner does not bypass",
        refuse_function_owner,
        "harrier_schedule_inputs() is owned by a role that does not bypass row security; "
        "it would list no owner",
    ),
    (
        "a store behind the code",
        refuse_old_store,
        "the store is not at the version this code needs",
    ),
    (
        "the service role key",
        lambda _rig: {"SUPABASE_SERVICE_ROLE_KEY": "synthetic"},
        "a service credential in SUPABASE_SERVICE_ROLE_KEY",
    ),
    (
        "a secret key under another name",
        lambda _rig: {"SOME_OTHER_NAME": "sb_secret_synthetic"},
        "a service credential in SOME_OTHER_NAME",
    ),
    (
        "no concurrency",
        lambda _rig: {CONCURRENCY_VARIABLE: None},
        f"{CONCURRENCY_VARIABLE} is missing",
    ),
    (
        "zero concurrency",
        lambda _rig: {CONCURRENCY_VARIABLE: "0"},
        f"{CONCURRENCY_VARIABLE} is missing or not a positive integer",
    ),
    ("text concurrency", lambda _rig: {CONCURRENCY_VARIABLE: "two"}, CONCURRENCY_VARIABLE),
    ("negative timeout", lambda _rig: {TIMEOUT_VARIABLE: "-5"}, TIMEOUT_VARIABLE),
    ("no timeout", lambda _rig: {TIMEOUT_VARIABLE: None}, f"{TIMEOUT_VARIABLE} is missing"),
    ("no run login", lambda _rig: {RUN_DATABASE_URL_VARIABLE: None}, RUN_DATABASE_URL_VARIABLE),
    (
        "the run login is the scheduler's own",
        lambda rig: {RUN_DATABASE_URL_VARIABLE: rig.scheduler.url},
        "login role of their own",
    ),
    (
        "a run login that can list schedules",
        refuse_run_login_with_the_reader,
        "member of harrier_schedule_reader",
    ),
    ("no credentials key", lambda _rig: {CREDENTIALS_KEY_VARIABLE: None}, CREDENTIALS_KEY_VARIABLE),
    (
        "a malformed key",
        lambda _rig: {CREDENTIALS_KEY_VARIABLE: "not-a-key"},
        CREDENTIALS_KEY_VARIABLE,
    ),
    (
        "a run login that is not a postgres url",
        lambda _rig: {RUN_DATABASE_URL_VARIABLE: "mysql://run@127.0.0.1/db"},
        f"{RUN_DATABASE_URL_VARIABLE} is missing or not a postgresql:// URL",
    ),
    ("a store without the listing function", refuse_no_function, "does not exist"),
    ("a store that skipped a migration", refuse_skipped_version, "version this code needs"),
    (
        "a login that is a member of the reader and cannot set it",
        refuse_reader_without_set,
        "cannot set role harrier_schedule_reader",
    ),
)


@pytest.mark.parametrize(("case", "setup", "named"), REFUSALS, ids=[case[0] for case in REFUSALS])
def test_the_scheduler_refuses_a_login_that_can_bypass(
    rig: Rig,
    capsys: pytest.CaptureFixture[str],
    case: str,
    setup: Callable[[Rig], dict[str, str | None]],
    named: str,
) -> None:
    """The scheduler refuses to start, exit 1, naming the check, for each
    check under "The scheduler's login": a login that can step past the
    policy or cannot take its two roles, a function whose owner would list
    no owner, a store behind the code, a service credential in the
    environment, and a missing limit. Nothing is listed or started, and no
    owner's row is written."""
    owner = rig.owner()
    overrides = setup(rig)
    assert rig.run(rig.environ(**overrides)) == 1, case
    err = capsys.readouterr().err
    assert named in err, (case, err)
    assert err.startswith("error: "), (case, err)
    assert rig.fleet.spawned == []
    assert rig.sql("SELECT count(*) FROM schedule_state") == [(0,)]
    assert owner not in err


@pytest.mark.parametrize(("concurrency", "timeout"), [(0, 60), (-1, 60), (2, 0)])
def test_a_scheduler_with_no_room_to_run_is_never_built(
    rig: Rig, concurrency: int, timeout: int
) -> None:
    """`read_settings` refuses a limit below 1, and is the one way production
    builds the settings. A scheduler built another way with a limit of 0 would
    queue slots it never starts and wait for ever, so it is refused when it is
    built, not left to hang."""
    key = parse_key(rig.key_text)
    assert key is not None
    settings = hostedscheduler.Settings(concurrency, timeout, rig.run_login.url, key)
    with pytest.raises(ValueError, match="at least 1"):
        Scheduler(
            connect=lambda: postgres_connect(rig.scheduler.url),
            jobs=rig.jobs,
            settings=settings,
            server_env=rig.environ(),
            root=rig.tmp_path,
        )


def test_a_clean_login_passes_every_check(rig: Rig) -> None:
    """The refusals above are not an environment that refuses everything:
    the rig's own login and limits start."""
    rig.owner()
    assert rig.run() == 0
    assert len(rig.fleet.spawned) == 1


def test_the_service_role_key_check_names_the_variable_not_the_value(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = f"sb_secret_{uuid.uuid4().hex}"
    assert rig.run(rig.environ(NOT_A_KEY_NAME=secret)) == 1
    err = capsys.readouterr().err
    assert "NOT_A_KEY_NAME" in err and secret not in err


# --- the API and the run login refuse the reader ---


def test_the_api_login_cannot_list_schedules(server: Server) -> None:
    """The API refuses to start when its login is a member of
    `harrier_schedule_reader`: the cross-owner listing never sits behind an
    HTTP route. A login that is not passes (spec 108, Scope item 9)."""
    plain = server.login_role()
    open_request_store(plain.url).close()

    listing = scheduler_login(server)
    with pytest.raises(HostedStartRefused) as refused:
        open_request_store(listing.url)
    assert str(refused.value) == (
        "the hosted API refuses its login role: member of harrier_schedule_reader"
    )
    # Through another role, which a membership reaches all the same.
    through = server.login_role()
    group = server.role("NOLOGIN")
    server.sql(f"GRANT {SCHEDULE_READER_ROLE} TO {group}", f"GRANT {group} TO {through.name}")
    with pytest.raises(HostedStartRefused, match="member of harrier_schedule_reader"):
        open_request_store(through.url)
    # And a run login, the process that runs domain code.
    with pytest.raises(HostedStartRefused, match="member of harrier_schedule_reader"):
        check_run_login_role(listing.url, "someone_else")
    check_run_login_role(plain.url, "someone_else")


# --- the child's clock ---


def test_a_scheduled_run_reads_the_owners_clock(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """The child's environment carries the owner's `TZ`, and a child started
    with it reads the owner's local hour, so the Apify gate opens on the
    owner's weekday mornings (spec 108, "Claiming and starting a run as its
    owner")."""
    monkeypatch.setattr(hostedscheduler, "POLL_SECONDS", 0.05)

    script = (
        "import sys\n"
        "from datetime import datetime\n"
        "from harrier.discovery import apify_allowed_now\n"
        "now = datetime.now()\n"
        # A relative path: the file lands in the child's working directory,
        # which must be its owner's run directory.
        "open('result', 'w').write(f'{now.hour} {apify_allowed_now(now)}')\n"
    )
    zones = ("Pacific/Kiritimati", "Pacific/Pago_Pago")
    owners = {rig.owner(timezone=zone): zone for zone in zones}
    # The child imports harrier, so it needs this interpreter's own paths.
    real_env = {"PATH": os.environ.get("PATH", "")}

    def spawn(argv: Sequence[str], env: Mapping[str, str], cwd: Path) -> Process:
        return spawn_group([sys.executable, "-c", script], {**real_env, **env}, cwd)

    before = {zone: datetime.now(ZoneInfo(zone)) for zone in zones}
    assert rig.run(clock=RealTimeClock(), spawn=spawn) == 0
    after = {zone: datetime.now(ZoneInfo(zone)) for zone in zones}

    assert rig.outcomes() == dict.fromkeys(owners, "ok")
    for owner, zone in owners.items():
        hour, allowed = (rig.tmp_path / "owners" / owner / "result").read_text().split()
        # The child read its clock between `before` and `after`.
        assert int(hour) in {before[zone].hour, after[zone].hour}, zone
        local = before[zone].replace(tzinfo=None)
        if before[zone].hour == after[zone].hour:
            assert allowed == str(apify_allowed_now(local)), zone
    # Two zones 25 hours apart read different local clocks.
    assert owners and len(set(owners.values())) == 2
    hours = {
        zone: int((rig.tmp_path / "owners" / o / "result").read_text().split()[0])
        for o, zone in owners.items()
    }
    assert hours[zones[0]] != hours[zones[1]]


# --- logs ---

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
STAMP = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
# Every line the scheduler may write (spec 108, "Logs"), and nothing else.
ALLOWED_LOGS = (
    rf"scheduled start owner={UUID} job=[a-z-]+ slot={STAMP}",
    rf"scheduled end owner={UUID} job=[a-z-]+ slot={STAMP} outcome=[a-z_]+ exit=(-?\d+|-)",
    r"scheduler standing by: another instance holds the lock",
    r"scheduler lost its lock",
    r"scheduler stopped",
    rf"scheduled skip owner={UUID} reason=[^ ].*",
    rf"scheduled step failed owner={UUID}( job=[a-z-]+)? step=[a-z]+ error=\w+(: .*)?",
)


def test_scheduler_logs_name_only_the_owner_id(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    """No scheduler log line contains anything but the fields under Logs. A
    run's output appears in no log and in no row, and an exception is logged
    by class, with its text only when harrier wrote it (spec 108, "Logs")."""
    caplog.set_level(logging.DEBUG)
    marker = f"OUTPUT-{uuid.uuid4().hex}"
    leak = f"LEAK-{uuid.uuid4().hex}"
    ok, failing, refused, bad_extra = sorted(rig.owner() for _ in range(4))
    rig.fleet.refusals = {
        refused: RuntimeError(f"holds a value: {leak}"),
        bad_extra: ExtraVariableRefused("a run's environment does not take PATH"),
    }
    rig.fleet.plans = {failing: Plan(seconds=2, code=3)}
    # A megabyte on each stream: a child whose output went to a pipe nobody
    # reads would block on it and never finish.
    script = (
        f"import sys; sys.stdout.write({marker!r} * 50000); sys.stderr.write({marker!r} * 50000)"
    )

    def noisy(argv: Sequence[str], env: Mapping[str, str], cwd: Path) -> Process:
        # A real child that prints to both streams, then whatever the plan
        # says; a fake one for the rest.
        if env["OWNER"] == ok:
            noise = spawn_group([sys.executable, "-c", script], dict(env), cwd)
            deadline = time.monotonic() + 20
            while noise.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            assert noise.poll() == 0, "a child that prints a lot did not finish"
        return rig.fleet.spawn(argv, env, cwd)

    assert rig.run(spawn=noisy) == 0

    messages = [
        r.getMessage()
        for r in caplog.records
        if r.name in (SCHEDULER_LOGGER, "harrier.hostedschedule")
    ]
    assert messages, "the tick logged nothing"
    for message in messages:
        assert any(re.fullmatch(pattern, message) for pattern in ALLOWED_LOGS), message
    text = "\n".join(r.getMessage() + (r.exc_text or "") for r in caplog.records)
    assert marker not in text and leak not in text
    assert "RuntimeError" in text and "ExtraVariableRefused" in text
    # Nor does the store hold a run's output.
    stored = json.dumps(rig.sql("SELECT * FROM schedule_state"), default=str)
    assert marker not in stored and leak not in stored
    # Every owner is named by id, and each run that started and ended is
    # logged once each way.
    started = [m for m in messages if m.startswith("scheduled start")]
    ended = [m for m in messages if m.startswith("scheduled end")]
    assert len(started) == 2 and len(ended) == 4


# --- what the local reviews of PR 327 found ---


@pytest.mark.parametrize("number", [signal.SIGINT, signal.SIGTERM])
def test_a_real_signal_stops_the_tick(rig: Rig, number: signal.Signals) -> None:
    """Fly sends SIGINT and SIGTERM, a second apart, when an update replaces
    the machine under a running tick. Sent for real to this process, each ends
    the tick as a stop does: the children are terminated and recorded
    `interrupted`, the exit status is 1, and the process's own handlers are
    back after (spec 108, Amendment (2026-10-12))."""
    owner = rig.owner()
    rig.fleet.plans = {owner: HANGS}
    rig.fleet.on_spawn = lambda _spawned: signal.raise_signal(number)
    before = signal.getsignal(number)

    assert rig.run(handle_signals=True) == 1

    assert rig.outcomes() == {owner: "interrupted"}
    assert rig.fleet.spawned[0].process.terminated
    assert signal.getsignal(number) == before


def test_a_run_nothing_can_end_does_not_hold_the_tick_for_ever(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """A process that outlives terminate and kill, as one outside its group
    would, is given up on after the grace: the row says `timed_out`, the
    scheduler logs that it could not confirm the end, and the tick finishes
    and lets go of the lock, where waiting for ever would make Fly skip every
    later start. The same on a stop (spec 108, "Finishing a run")."""
    caplog.set_level(logging.INFO, logger=SCHEDULER_LOGGER)
    unkillable = Plan(seconds=None, ignores_terminate=True, survives_kill=True)
    owner = rig.owner()
    rig.fleet.plans = {owner: unkillable}
    assert rig.run() == 0
    assert rig.outcomes() == {owner: "timed_out"}
    assert rig.fleet.spawned[0].process.killed
    assert f"scheduled step failed owner={owner} job=discovery step=kill error=not reaped" in (
        caplog.text
    )

    rig.sql("DELETE FROM schedule_state")
    stop = threading.Event()
    rig.fleet.spawned.clear()
    rig.fleet.on_spawn = lambda _spawned: stop.set()
    assert rig.run(stop=stop) == 1
    assert rig.outcomes() == {owner: "interrupted"}
    assert rig.fleet.spawned[0].process.killed


def test_a_hung_run_is_terminated_at_the_timeout_and_not_a_second_later(rig: Rig) -> None:
    owner = rig.owner()
    rig.fleet.plans = {owner: HANGS}
    assert rig.run() == 0
    process = rig.fleet.spawned[0].process
    assert process.terminated_at is not None
    assert process.terminated_at - process.started == 60


def test_the_lock_check_sees_only_this_session(rig: Rig) -> None:
    """`held()` asks `pg_locks` whether this backend holds the lock. While
    another session holds it, a fresh connection is told it does not: behind
    a transaction pooler the statement runs on a backend that does not, and
    the start is refused. The holder is told it does (spec 108, "One
    scheduler at a time")."""
    holder = hold_the_lock(rig)
    try:
        assert hostedscheduler.AdvisoryLock(Connection.postgres(holder)).held() is True
        with closing(postgres_connect(rig.scheduler.url)) as fresh:
            assert hostedscheduler.AdvisoryLock(Connection.postgres(fresh)).held() is False
    finally:
        holder.close()


def test_a_runs_output_reaches_no_stream_of_the_scheduler(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """On Fly the scheduler's own stdout and stderr are the log, so a child
    that inherited them would put its output there: nothing a run prints
    reaches the scheduler's streams (spec 108, Open decision 6)."""
    monkeypatch.setattr(hostedscheduler, "POLL_SECONDS", 0.05)
    marker = f"OUTPUT-{uuid.uuid4().hex}"
    script = f"import sys; print({marker!r}); print({marker!r}, file=sys.stderr)"
    owner = rig.owner()

    def spawn(argv: Sequence[str], env: Mapping[str, str], cwd: Path) -> Process:
        return spawn_group([sys.executable, "-c", script], env, cwd)

    assert rig.run(clock=RealTimeClock(), spawn=spawn) == 0
    assert rig.outcomes() == {owner: "ok"}
    captured = capfd.readouterr()
    assert marker not in captured.out and marker not in captured.err


def test_a_sweep_marks_only_running_rows(rig: Rig) -> None:
    """The sweep turns `running` into `interrupted` and touches nothing else:
    a failed run that is not due keeps its outcome and exit status, so the
    report still shows it."""
    owner = rig.owner(jobs=("discovery", "academic-discovery"), apify=True)
    rig.put_state(owner, "discovery", last_outcome="running", last_exit=None)
    rig.put_state(
        owner,
        "academic-discovery",
        last_slot_at=ACADEMIC_SLOT,
        last_outcome="failed",
        last_exit=3,
        last_finished_at="2026-10-12T09:45:00Z",
        last_ok_at="2026-10-05T09:50:00Z",
    )
    assert rig.run() == 0
    state = rig.state()
    kept = state[(owner, "academic-discovery")]
    assert (kept["last_outcome"], kept["last_exit"]) == ("failed", 3)
    assert kept["last_finished_at"] == "2026-10-12T09:45:00Z"
    assert state[(owner, "discovery")]["last_outcome"] == "interrupted"
    assert rig.fleet.spawned == []


def test_a_report_refuses_a_function_that_would_list_no_owner(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `harrier_schedule_inputs()` owned by a role that cannot bypass row
    security lists nobody, and the scheduler refuses to start. The report
    must say so, not print an empty schedule and exit 0: a scheduler that
    never starts is seen through it (spec 108, Failure modes, and Hosting
    budget). Built as the fault is, with a function owner that may read the
    tables and does not bypass the policy."""
    rig.owner(updated_at="2026-10-01 00:00:00")
    owner_role = rig.server.role("NOLOGIN")
    rig.server.sql(
        f"GRANT SELECT ON user_config, schedule_state TO {owner_role}",
        f"ALTER FUNCTION public.harrier_schedule_inputs() OWNER TO {owner_role}",
    )
    out = io.StringIO()
    code = run_report(rig.environ(), clock=FakeClock(), jobs=rig.jobs, out=out)
    assert code == 1
    assert out.getvalue() == ""
    assert "owned by a role that does not bypass row security" in capsys.readouterr().err


def test_a_report_exits_one_when_it_cannot_list(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    """A login that cannot take the reader's role cannot list, and the report
    says so with exit 1."""
    login = scheduler_login(rig.server, reader=False)
    out = io.StringIO()
    code = run_report(
        rig.environ(**{URL_VARIABLE: login.url}), clock=FakeClock(), jobs=rig.jobs, out=out
    )
    assert code == 1 and out.getvalue() == ""
    assert capsys.readouterr().err.startswith("error: ")


def test_a_store_error_is_logged_without_a_secret(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """A store error's text is scrubbed before it is logged."""
    secret = f"hunter-{uuid.uuid4().hex}"
    text = hostedscheduler.describe(
        connection.StoreDatabaseError(f"failed at postgresql://someone:{secret}@db.example/x")
    )
    assert secret not in text and text.startswith("StoreDatabaseError: ")


def test_one_unreadable_row_stops_neither_the_tick_nor_the_report(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """A `schedule` row that reaches the store another way and breaks the
    JSON parser itself (nested past its depth) is skipped and logged with the
    owner's id; the other owner's run starts, and the report shows the row
    instead of crashing on it (spec 108, "A tick", "Isolation")."""
    caplog.set_level(logging.INFO, logger="harrier.hostedschedule")
    good = rig.owner()
    poisoned = rig.owner()
    rig.sql(
        "UPDATE user_config SET value = %s WHERE kind = 'schedule' AND owner_id = %s",
        ("[" * 100_000, poisoned),
    )

    assert rig.run() == 0

    assert rig.outcomes() == {good: "ok"}
    assert [m for m in (r.getMessage() for r in caplog.records) if "scheduled skip" in m] == [
        f"scheduled skip owner={poisoned} reason=schedule is not valid JSON"
    ]
    code, text = report(rig, now=START + timedelta(minutes=10))
    assert code == 0, text
    assert f"{poisoned} - schedule invalid: schedule is not valid JSON" in text


def test_a_missed_slot_keeps_the_last_start(rig: Rig) -> None:
    """A slot recorded `missed` never started, so it leaves `last_started_at`
    alone: the queue orders owners by it, and an owner just marked missed must
    not jump ahead of owners who never started (spec 108, "A tick", step 3)."""
    owner = rig.server.owner()
    rig.put_state(
        owner,
        "discovery",
        last_slot_at="2026-10-11T20:00:00Z",
        last_started_at="2026-10-11T20:05:00Z",
    )
    with closing(postgres_connect(rig.scheduler.url)) as driver:
        conn = Connection.postgres(driver, claims=owner_claims(owner))
        assert claimed(conn, "discovery", SLOT, outcome="missed") is True
    row = rig.state()[(owner, "discovery")]
    assert (row["last_slot_at"], row["last_outcome"]) == (SLOT, "missed")
    assert row["last_started_at"] == "2026-10-11T20:05:00Z"
    assert row["last_ok_at"] == SLOT


def test_an_outcome_no_row_holds_is_not_a_clean_tick(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """The owner's row was deleted while the run ran, as erasing an owner
    does: the finish changes no row, so nothing recorded the outcome. It is
    logged and the tick exits 1, as for a finish the store refused."""
    caplog.set_level(logging.INFO, logger=SCHEDULER_LOGGER)
    owner = rig.owner()

    def erase(_spawned: Spawned) -> None:
        rig.sql("DELETE FROM schedule_state")

    rig.fleet.on_spawn = erase
    assert rig.run() == 1
    assert rig.state() == {}
    assert f"scheduled step failed owner={owner} job=discovery step=finish error=LookupError" in (
        caplog.text
    )
    assert "scheduled end" not in caplog.text


def test_a_log_or_a_repr_never_carries_the_passwords_or_the_key(rig: Rig) -> None:
    """The settings and the scheduler hold both database URLs and the
    credentials key in their fields; none of it is in their text, so a log
    line, a `%r` or a crash report that shows one carries no secret."""
    environ = rig.environ()
    key = parse_key(rig.key_text)
    assert key is not None
    settings = read_settings(environ)
    scheduler = Scheduler(
        connect=lambda: postgres_connect(rig.scheduler.url),
        jobs=rig.jobs,
        settings=settings,
        server_env=environ,
        root=rig.tmp_path,
    )
    secrets = [rig.scheduler.password, rig.run_login.password, rig.key_text.split(":")[1]]
    for text in (repr(settings), repr(scheduler), str(settings)):
        assert not any(secret in text for secret in secrets), text


def test_an_unreadable_credential_is_logged_with_its_key_id(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """An operator needs the key id to see a replaced key (spec 106 logs it
    the same way); the value is never in the line."""
    caplog.set_level(logging.INFO, logger=SCHEDULER_LOGGER)
    owner = rig.owner()
    rig.fleet.refusals = {owner: CredentialUnreadable("apify", "old-key-id")}
    assert rig.run() == 0
    assert rig.outcomes() == {owner: "refused"}
    assert (
        f"scheduled step failed owner={owner} job=discovery step=bind "
        "error=CredentialUnreadable: your apify credential cannot be read; set it again "
        "key_id=old-key-id"
    ) in caplog.text


def test_a_run_login_refusal_speaks_for_the_scheduler(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    """The check is the API's; the words an operator reads name the
    scheduler, not the hosted API."""
    assert rig.run(rig.environ(**{RUN_DATABASE_URL_VARIABLE: rig.scheduler.url})) == 1
    err = capsys.readouterr().err
    assert "hosted API" not in err and "API's own" not in err
    assert "the scheduler's own login role" in err


# --- the operator's report ---


def report(rig: Rig, now: datetime = datetime(2026, 10, 14, 12, 30, tzinfo=UTC)) -> tuple[int, str]:
    out = io.StringIO()
    code = run_report(rig.environ(), clock=FakeClock(now), jobs=rig.jobs, out=out)
    return code, out.getvalue()


def clear(rig: Rig) -> None:
    rig.sql("DELETE FROM schedule_state")
    rig.sql("DELETE FROM user_config WHERE kind = 'schedule'")


def test_the_report_exits_one_on_each_problem(rig: Rig) -> None:
    """`schedule-report` exits 0 with nothing wrong and 1 for each problem,
    naming it; `not_configured`, `interrupted` and `missed` alone exit 0.
    With no tick running and no slot unclaimed it prints `scheduler: no tick
    running` (spec 108, "The operator's report")."""
    recent = "2026-10-14T09:00:00Z"
    owner = rig.owner(jobs=("discovery",), updated_at="2026-10-01 00:00:00")
    rig.put_state(owner, "discovery", last_slot_at=recent, last_ok_at=recent)
    code, text = report(rig)
    assert code == 0, text
    assert text.splitlines()[0] == "scheduler: no tick running"
    assert text.splitlines()[1].startswith(f"{owner} discovery cadence=")
    assert "PROBLEM" not in text

    for outcome in ("not_configured", "interrupted", "missed"):
        clear(rig)
        owner = rig.owner(updated_at="2026-10-01 00:00:00")
        rig.put_state(
            owner,
            "discovery",
            last_slot_at=recent,
            last_ok_at=recent,
            last_outcome=outcome,
            last_exit=None,
        )
        assert report(rig)[0] == 0, outcome

    problems = {
        "failed": {"last_outcome": "failed", "last_exit": 3},
        "crashed": {"last_outcome": "crashed", "last_exit": 1},
        "timed_out": {"last_outcome": "timed_out", "last_exit": None},
        "refused": {"last_outcome": "refused", "last_exit": None},
        "overdue": {"last_ok_at": "2026-10-10T09:00:00Z"},
        "a whole slot passed unclaimed": {
            "last_slot_at": "2026-10-13T20:00:00Z",
            "last_ok_at": "2026-10-13T20:05:00Z",
        },
    }
    for named, columns in problems.items():
        clear(rig)
        owner = rig.owner(updated_at="2026-10-01 00:00:00")
        rig.put_state(
            owner, "discovery", **{"last_slot_at": recent, "last_ok_at": recent, **columns}
        )
        code, text = report(rig)
        assert code == 1, (named, text)
        line = next(line for line in text.splitlines() if line.startswith(owner))
        assert f"PROBLEM: {named}" in line or named in line.split("PROBLEM:")[1], (named, line)

    # Never ok, chosen long ago, no state at all: overdue.
    clear(rig)
    owner = rig.owner(updated_at="2026-10-01 00:00:00")
    code, text = report(rig)
    assert code == 1 and "overdue: never ok since chosen" in text
    # An owner who stopped the job is not reported for it.
    rig.sql(
        "UPDATE user_config SET value = %s WHERE kind = 'schedule'",
        (json.dumps({"timezone": "UTC", "jobs": []}),),
    )
    assert report(rig)[0] == 0


def test_the_report_says_when_a_tick_is_running(rig: Rig) -> None:
    holder = hold_the_lock(rig)
    try:
        code, text = report(rig)
    finally:
        holder.close()
    assert (code, text.splitlines()[0]) == (0, "scheduler: a tick is running")
    assert report(rig)[1].splitlines()[0] == "scheduler: no tick running"


def test_a_lock_in_another_database_is_not_a_tick_here(rig: Rig, server: Server) -> None:
    """Advisory locks belong to a database and `pg_locks` lists the
    cluster's: another database's scheduler is no tick of this store."""
    with psycopg.connect(server.url.rsplit("/", 1)[0] + "/postgres", autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(hashtext('harrier.scheduler'), 0)")
        assert report(rig)[1].splitlines()[0] == "scheduler: no tick running"
