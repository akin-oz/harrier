"""The hosted schedule's store (spec 108, the store's pull request).

An owner's choice of scheduled jobs and timezone is the `schedule` kind of
their configuration, a kind only the hosted store holds. The scheduler lists
every owner's choice and state through one function, as a role that reaches
nothing else past the owner policy.

Owners are random UUIDs and every value is synthetic. The Postgres tests
need a server and skip locally without one (`tests/pg_support.py`).
"""

# Pyright strict cannot resolve starlette's TestClient request and response
# members, which is why every API test file carries these.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest
from conftest import auth
from fastapi.testclient import TestClient
from pg_support import as_owner, as_role, database_copy, new_owner, open_owner_store
from test_hosted_requests import Hosted, Server, copied_server, serving

from harrier.connection import Connection
from harrier.hostedschedule import HOSTED_JOBS
from harrier.pgstore import postgres_connect
from harrier.schedule import load_schedule
from harrier.tracker.schema import SCHEDULE_READER_ROLE, TENANT_ROLE
from harrier.userconfig import (
    DISCOVERY,
    FEEDS,
    KINDS,
    SCHEDULE,
    ConfigError,
    delete_config,
    get_config,
    kinds_for,
    set_config,
)
from harrier_api.app import create_app

if TYPE_CHECKING:
    import psycopg

    PgConnection = psycopg.Connection[Any]

INSUFFICIENT_PRIVILEGE = "42501"

VALID = {"timezone": "Europe/Amsterdam", "jobs": ["discovery", "academic-discovery"]}

# A key an owner might write, synthetic. A refusal counts it, never names it.
OWNER_KEY = "Sam Example sam@example.com"

# Each refusal under spec 108's "The owner's choice", and its whole
# message, which names the field. Nothing the owner wrote is in the message: no value, and
# no unknown field's name (OWNER_TEXT).
NOT_A_ZONE = "schedule.timezone is not a zone this server can load"
NOT_A_HOSTED_JOB = (
    "schedule.jobs has an entry that is not a hosted job; "
    "expected entries from ['discovery', 'academic-discovery']"
)
REFUSALS: tuple[tuple[str, object, str], ...] = (
    ("not an object", ["discovery"], "schedule must be a JSON object, got list"),
    (
        "another field",
        {**VALID, "times": ["09:00"]},
        "schedule has 1 unknown field; expected only ['jobs', 'timezone']",
    ),
    (
        "two other fields",
        {**VALID, "times": ["09:00"], OWNER_KEY: 1},
        "schedule has 2 unknown fields; expected only ['jobs', 'timezone']",
    ),
    ("no timezone", {"jobs": []}, "schedule.timezone is required"),
    ("no jobs", {"timezone": "UTC"}, "schedule.jobs is required"),
    ("an unknown zone", {**VALID, "timezone": "Mars/Olympus_Mons"}, NOT_A_ZONE),
    ("a zone directory", {**VALID, "timezone": "Europe"}, NOT_A_ZONE),
    ("a path", {**VALID, "timezone": "../../etc/passwd"}, NOT_A_ZONE),
    ("a zone that is not text", {**VALID, "timezone": 1}, NOT_A_ZONE),
    (
        "jobs not a list",
        {**VALID, "jobs": "discovery"},
        "schedule.jobs must be a JSON list, got str",
    ),
    ("a local-only job", {**VALID, "jobs": ["gmail-watch"]}, NOT_A_HOSTED_JOB),
    ("a job not yet hosted", {**VALID, "jobs": ["digest"]}, NOT_A_HOSTED_JOB),
    ("a job that is not text", {**VALID, "jobs": [7]}, NOT_A_HOSTED_JOB),
    (
        "a repeated job",
        {**VALID, "jobs": ["discovery", "discovery"]},
        "schedule.jobs repeats an entry",
    ),
)


# What an owner wrote in REFUSALS that is not a field name or a hosted job.
OWNER_TEXT = ("Mars", "passwd", "gmail-watch", "digest", "09:00", "times", "Sam", "example.com")


# --- the kind ---


@pytest.fixture()
def db(store: Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Connection:
    """The store, on both stores (spec 126), with no config/ tree in the
    working directory."""
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    monkeypatch.chdir(tmp_path)
    return store


def test_the_schedule_kind_is_hosted_only(db: Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """On a Postgres connection the kind is accepted with a valid value and
    refused, naming the field, for each case spec 108 lists. On SQLite it
    is an unknown kind with today's message, so the local product does not
    change (spec 108, Scope item 4). No refusal quotes or chains what the
    owner wrote."""
    if db.dialect == "sqlite":
        assert kinds_for(db) == KINDS
        unknown = f"unknown configuration kind 'schedule'; expected one of {KINDS}"
        with pytest.raises(ConfigError) as refused:
            set_config(db, SCHEDULE, VALID)
        assert str(refused.value) == unknown
        # Nothing was written, so a read finds no row, as for any kind.
        assert get_config(db, SCHEDULE) is None
        return

    assert kinds_for(db) == (*KINDS, SCHEDULE)
    set_config(db, SCHEDULE, VALID)
    assert get_config(db, SCHEDULE) == VALID
    # An empty list pauses the schedule; it is a choice, not a refusal.
    set_config(db, SCHEDULE, {"timezone": "Asia/Tokyo", "jobs": []})
    assert get_config(db, SCHEDULE) == {"timezone": "Asia/Tokyo", "jobs": []}

    for case, value, named in REFUSALS:
        with pytest.raises(ConfigError) as refused:
            set_config(db, SCHEDULE, value)
        message = str(refused.value)
        assert message == named, (case, message)
        assert not any(text in message for text in OWNER_TEXT), (case, message)
        # Nor does any exception the refusal carries, which a traceback in
        # a log would print: zoneinfo's own error quotes the zone.
        carried = [refused.value.__cause__, refused.value.__context__]
        assert carried == [None, None], (case, carried)
    # Nothing refused was written.
    assert get_config(db, SCHEDULE) == {"timezone": "Asia/Tokyo", "jobs": []}

    # A key naming a directory of the tz database raises IsADirectoryError
    # on some systems and ZoneInfoNotFoundError on others, so the OSError
    # branch is reached here by a ZoneInfo that raises it, quoting the key.
    import zoneinfo

    def directory(key: str) -> zoneinfo.ZoneInfo:
        raise IsADirectoryError(21, "Is a directory", f"/usr/share/zoneinfo/{key}")

    monkeypatch.setattr(zoneinfo, "ZoneInfo", directory)
    with pytest.raises(ConfigError) as refused:
        set_config(db, SCHEDULE, {**VALID, "timezone": "Europe"})
    assert str(refused.value) == NOT_A_ZONE
    assert [refused.value.__cause__, refused.value.__context__] == [None, None]
    monkeypatch.undo()

    assert delete_config(db, SCHEDULE) is True
    assert get_config(db, SCHEDULE) is None


def test_the_hosted_jobs_are_committed_jobs() -> None:
    """Each hosted job keeps its committed cadence and command, so each is
    a job `config/schedule.json` names (spec 108, The hosted job set)."""
    _, jobs = load_schedule(Path(__file__).resolve().parents[3] / "config" / "schedule.json")
    assert set(HOSTED_JOBS) <= {job.name for job in jobs}
    assert HOSTED_JOBS == ("discovery", "academic-discovery")


# --- the routes ---


@pytest.fixture(name="server")
def server_fixture(postgres_template: str) -> Iterator[Server]:
    yield from copied_server(postgres_template)


@pytest.fixture(name="hosted")
def hosted_fixture(server: Server, monkeypatch: pytest.MonkeyPatch) -> Iterator[Hosted]:
    """The hosted app, as spec 134's tests build it, on this test's copy."""
    with serving(monkeypatch, server.login_role().url) as built:
        yield built


def test_the_routes_serve_the_kind_hosted(server: Server, hosted: Hosted) -> None:
    """The /config routes serve the kinds the request's connection allows,
    so hosted an owner writes, reads, lists and deletes their schedule; a
    refused value is a 400 naming the field."""
    owner = server.owner()
    client = hosted.client()
    headers = hosted.headers(owner)

    put = client.put(f"/config/{SCHEDULE}", json={"value": VALID}, headers=headers)
    assert put.status_code == 200, put.text
    assert (put.json()["value"], put.json()["source"]) == (VALID, "store")

    listed = client.get("/config", headers=headers)
    assert listed.status_code == 200, listed.text
    assert [entry["kind"] for entry in listed.json()] == [*KINDS, SCHEDULE]

    bad = client.put(
        f"/config/{SCHEDULE}", json={"value": {**VALID, "jobs": ["digest"]}}, headers=headers
    )
    assert bad.status_code == 400, bad.text
    assert bad.json()["detail"] == NOT_A_HOSTED_JOB

    deleted = client.delete(f"/config/{SCHEDULE}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["value"] is None
    got = client.get(f"/config/{SCHEDULE}", headers=headers)
    assert (got.status_code, got.json()["value"]) == (200, None)


@pytest.mark.sqlite_only("drives the local API, which serves SQLite")
def test_the_local_routes_do_not_know_the_kind(db: Connection) -> None:
    client = TestClient(create_app())
    for response in (
        client.get(f"/config/{SCHEDULE}", headers=auth()),
        client.put(f"/config/{SCHEDULE}", json={"value": VALID}, headers=auth()),
        client.delete(f"/config/{SCHEDULE}", headers=auth()),
    ):
        assert response.status_code == 404, response.text
    assert {entry["kind"] for entry in client.get("/config", headers=auth()).json()} == set(KINDS)


# --- the listing ---

# harrier_schedule_inputs()'s columns, as spec 108 declares them.
INPUT_COLUMNS = [
    "owner_id",
    "schedule",
    "schedule_updated_at",
    "job",
    "last_slot_at",
    "last_started_at",
    "last_finished_at",
    "last_outcome",
    "last_exit",
    "last_ok_at",
]


@pytest.fixture()
def listing_store(postgres_template: str) -> Iterator[str]:
    """A copy of the migrated template whose migrating role may SET ROLE to
    the reader, as `open_staging` grants the tenant role: a migrator that
    did not create the role holds only the admin option on it."""
    with database_copy(postgres_template) as url:
        with closing(postgres_connect(url)) as conn:
            conn.execute(
                f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_auth_members
                        WHERE roleid = '{SCHEDULE_READER_ROLE}'::regrole
                            AND member = current_user::regrole AND set_option
                    ) THEN
                        EXECUTE format(
                            'GRANT {SCHEDULE_READER_ROLE} TO %I WITH INHERIT FALSE, SET TRUE',
                            current_user
                        );
                    END IF;
                END
                $$
                """.encode()
            )
        yield url


def refusal(conn: PgConnection, statement: str) -> Any:
    """The error `statement` raises, run in a savepoint so the transaction
    around it goes on. Fails the test if the statement is accepted."""
    import psycopg

    try:
        with conn.transaction():
            conn.execute(statement.encode())
    except psycopg.Error as error:
        return error
    pytest.fail(f"accepted, expected a refusal: {statement}")


# One state row per owner and job, with a distinct value in every column,
# so a column returned in another's place, or as null, changes the row.
STATES: tuple[tuple[str, str, str, str, str, int | None, str], ...] = (
    # (job, last_slot_at, last_started_at, last_finished_at, last_outcome,
    #  last_exit, last_ok_at)
    (
        "discovery",
        "2026-10-12T07:00:00Z",
        "2026-10-12T07:01:00Z",
        "2026-10-12T07:09:00Z",
        "ok",
        0,
        "2026-10-11T07:08:00Z",
    ),
    (
        "academic-discovery",
        "2026-10-05T07:00:00Z",
        "2026-10-05T07:02:00Z",
        "2026-10-05T07:30:00Z",
        "failed",
        3,
        "2026-09-28T07:31:00Z",
    ),
    (
        "discovery",
        "2026-10-12T11:00:00Z",
        "2026-10-12T11:03:00Z",
        "2026-10-12T11:20:00Z",
        "crashed",
        1,
        "2026-10-12T07:40:00Z",
    ),
)


def write_state(conn: PgConnection, owner: str, state: tuple[object, ...]) -> None:
    with as_owner(conn, owner):
        conn.execute(
            "INSERT INTO schedule_state (job, last_slot_at, last_started_at, last_finished_at, "
            "last_outcome, last_exit, last_ok_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            state,
        )


def test_only_the_reader_lists_schedules(listing_store: str) -> None:
    """As harrier_schedule_reader the function returns every owner's
    schedule and state, each column from its own source, once: an owner
    with both, one with a schedule alone, one with state alone. It returns
    no other kind and no other table, though each owner also has a
    watchlist and discovery settings. As the tenant role, anon,
    authenticated and service_role, calling it is refused, and the reader
    itself cannot read user_config or schedule_state directly."""
    import psycopg

    other_schedule = {"timezone": "Asia/Tokyo", "jobs": ["discovery"]}
    with psycopg.connect(listing_store, autocommit=True) as conn:
        both, schedule_only, state_only = new_owner(conn), new_owner(conn), new_owner(conn)
    for owner, schedule in ((both, VALID), (schedule_only, other_schedule), (state_only, None)):
        with closing(open_owner_store(listing_store, owner)) as store:
            set_config(store, FEEDS, ["https://boards.greenhouse.io/exampleco"])
            set_config(store, DISCOVERY, {"synthetic_setting": "kept-out"})
            if schedule is not None:
                set_config(store, SCHEDULE, schedule)

    with psycopg.connect(listing_store, autocommit=True) as conn:
        write_state(conn, both, STATES[0])
        write_state(conn, both, STATES[1])
        write_state(conn, state_only, STATES[2])
        # Read past the policy as the migrating role, which bypasses it.
        updated = {
            str(owner): str(at)
            for owner, at in conn.execute(
                "SELECT owner_id, updated_at FROM user_config WHERE kind = 'schedule'"
            ).fetchall()
        }

        with as_role(conn, SCHEDULE_READER_ROLE):
            cursor = conn.execute("SELECT * FROM harrier_schedule_inputs()")
            columns = [column.name for column in cursor.description or ()]
            rows = cursor.fetchall()
            for table in ("user_config", "schedule_state", "jobs"):
                error = refusal(conn, f"SELECT 1 FROM {table}")
                assert error.sqlstate == INSUFFICIENT_PRIVILEGE, table
                assert error.diag.message_primary == f"permission denied for table {table}"

        refused: dict[str, tuple[str | None, str | None]] = {}
        for role in (TENANT_ROLE, "anon", "authenticated", "service_role"):
            with as_role(conn, role, both):
                error = refusal(conn, "SELECT * FROM harrier_schedule_inputs()")
                refused[role] = (error.sqlstate, error.diag.message_primary)

    assert columns == INPUT_COLUMNS
    assert set(updated) == {both, schedule_only}
    ours = {UUID(both), UUID(schedule_only), UUID(state_only)}
    found = sorted(
        (
            (str(row[0]), json.loads(row[1]) if row[1] is not None else None, *row[2:])
            for row in rows
            if row[0] in ours
        ),
        key=repr,
    )
    none = (None,) * 7
    assert found == sorted(
        [
            (both, VALID, updated[both], *STATES[0]),
            (both, VALID, updated[both], *STATES[1]),
            (schedule_only, other_schedule, updated[schedule_only], *none),
            (state_only, None, None, *STATES[2]),
        ],
        key=repr,
    )
    # No row carries another kind's value.
    assert not any("greenhouse" in repr(row) or "kept-out" in repr(row) for row in rows)
    assert refused == dict.fromkeys(
        (TENANT_ROLE, "anon", "authenticated", "service_role"),
        (INSUFFICIENT_PRIVILEGE, "permission denied for function harrier_schedule_inputs"),
    )


# --- the table ---

# schedule_state's columns as spec 108's DDL declares them, as
# (name, type, nullable), in order. owner_id is the owned-table catalog's.
STATE_COLUMNS = [
    ("owner_id", "uuid", False),
    ("job", "text", False),
    ("last_slot_at", "text", False),
    ("last_started_at", "text", True),
    ("last_finished_at", "text", True),
    ("last_outcome", "text", False),
    ("last_exit", "integer", True),
    ("last_ok_at", "text", True),
]

# The outcomes spec 108's DDL lists, written out here rather than read from
# schema.SCHEDULE_OUTCOMES, so a change to either is seen.
SPEC_OUTCOMES = (
    "running",
    "ok",
    "failed",
    "crashed",
    "timed_out",
    "not_configured",
    "refused",
    "interrupted",
    "missed",
)
CHECK_VIOLATION = "23514"
NOT_NULL = "23502"


def test_schedule_state_has_the_specs_columns(listing_store: str) -> None:
    """The table's columns, types and nullability are the spec's DDL; each
    outcome the spec lists is stored, any other is refused by the CHECK,
    and a slot is required."""
    import psycopg

    with psycopg.connect(listing_store, autocommit=True) as conn:
        owner = new_owner(conn)
        shape = [
            (str(name), str(kind), nullable == "YES")
            for name, kind, nullable in conn.execute(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'schedule_state' "
                "ORDER BY ordinal_position"
            ).fetchall()
        ]
        insert = "INSERT INTO schedule_state (job, last_slot_at, last_outcome) VALUES (%s, %s, %s)"
        with as_owner(conn, owner):
            for number, outcome in enumerate(SPEC_OUTCOMES):
                conn.execute(insert, (f"job-{number}", "2026-10-12T07:00:00Z", outcome))
            stored = conn.execute("SELECT count(*) FROM schedule_state").fetchone()
            refusals: list[tuple[str | None, str | None]] = []
            for params in (
                ("other", "2026-10-12T07:00:00Z", "paused"),
                ("other", None, "ok"),
            ):
                try:
                    with conn.transaction():
                        conn.execute(insert, params)
                except psycopg.Error as error:
                    refusals.append((error.sqlstate, error.diag.column_name))
                else:
                    refusals.append((None, None))
    assert shape == STATE_COLUMNS
    assert stored == (len(SPEC_OUTCOMES),)
    assert refusals == [(CHECK_VIOLATION, None), (NOT_NULL, "last_slot_at")]
