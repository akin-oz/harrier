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

# Each refusal under spec 108's "The owner's choice", and the field its
# message names. Nothing the owner wrote is in the message: no value, and
# no unknown field's name (OWNER_TEXT).
REFUSALS: tuple[tuple[str, object, str], ...] = (
    ("not an object", ["discovery"], "schedule must be a JSON object"),
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
    ("an unknown zone", {**VALID, "timezone": "Mars/Olympus_Mons"}, "schedule.timezone"),
    ("a zone directory", {**VALID, "timezone": "Europe"}, "schedule.timezone"),
    ("a path", {**VALID, "timezone": "../../etc/passwd"}, "schedule.timezone"),
    ("a zone that is not text", {**VALID, "timezone": 1}, "schedule.timezone"),
    ("jobs not a list", {**VALID, "jobs": "discovery"}, "schedule.jobs must be a JSON list"),
    ("a local-only job", {**VALID, "jobs": ["gmail-watch"]}, "schedule.jobs has an entry"),
    ("a job not yet hosted", {**VALID, "jobs": ["digest"]}, "schedule.jobs has an entry"),
    ("a job that is not text", {**VALID, "jobs": [7]}, "schedule.jobs has an entry"),
    ("a repeated job", {**VALID, "jobs": ["discovery", "discovery"]}, "schedule.jobs repeats"),
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


def test_the_schedule_kind_is_hosted_only(db: Connection) -> None:
    """On a Postgres connection the kind is accepted with a valid value and
    refused, naming the field, for each case spec 108 lists. On SQLite it
    is an unknown kind with today's message, so the local product does not
    change (spec 108, Scope item 4)."""
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
        assert message.startswith(named), (case, message)
        assert not any(text in message for text in OWNER_TEXT), (case, message)
        # Nor does any exception the refusal carries, which a traceback in
        # a log would print: zoneinfo's own error quotes the zone.
        carried = [refused.value.__cause__, refused.value.__context__]
        assert carried == [None, None], (case, carried)
    # Nothing refused was written.
    assert get_config(db, SCHEDULE) == {"timezone": "Asia/Tokyo", "jobs": []}

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
    assert bad.json()["detail"].startswith("schedule.jobs has an entry")

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


def write_state(conn: PgConnection, owner: str, job: str, outcome: str) -> None:
    with as_owner(conn, owner):
        conn.execute(
            "INSERT INTO schedule_state (job, last_slot_at, last_outcome) VALUES (%s, %s, %s)",
            (job, "2026-10-12T07:00:00Z", outcome),
        )


def test_only_the_reader_lists_schedules(listing_store: str) -> None:
    """As harrier_schedule_reader the function returns every owner's
    schedule and state: an owner with both, one with a schedule alone, one
    with state alone. It returns no other kind and no other table, though
    each owner also has a watchlist. As the tenant role, anon,
    authenticated and service_role, calling it is refused, and the reader
    itself cannot read user_config or schedule_state directly."""
    import psycopg

    with psycopg.connect(listing_store, autocommit=True) as conn:
        both, schedule_only, state_only = new_owner(conn), new_owner(conn), new_owner(conn)
    for owner, schedule in (
        (both, VALID),
        (schedule_only, {"timezone": "Asia/Tokyo", "jobs": ["discovery"]}),
        (state_only, None),
    ):
        with closing(open_owner_store(listing_store, owner)) as store:
            set_config(store, FEEDS, ["https://boards.greenhouse.io/exampleco"])
            if schedule is not None:
                set_config(store, SCHEDULE, schedule)

    with psycopg.connect(listing_store, autocommit=True) as conn:
        write_state(conn, both, "discovery", "ok")
        write_state(conn, both, "academic-discovery", "failed")
        write_state(conn, state_only, "discovery", "interrupted")

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
    found = {
        (str(row[0]), row[3]): (json.loads(row[1]) if row[1] is not None else None, row[7])
        for row in rows
        if UUID(str(row[0])) in {UUID(both), UUID(schedule_only), UUID(state_only)}
    }
    assert found == {
        (both, "discovery"): (VALID, "ok"),
        (both, "academic-discovery"): (VALID, "failed"),
        (schedule_only, None): ({"timezone": "Asia/Tokyo", "jobs": ["discovery"]}, None),
        (state_only, "discovery"): (None, "interrupted"),
    }
    # No row carries another kind's value: the watchlist is in none.
    assert not any("greenhouse" in repr(row) for row in rows)
    assert refused == dict.fromkeys(
        (TENANT_ROLE, "anon", "authenticated", "service_role"),
        (INSUFFICIENT_PRIVILEGE, "permission denied for function harrier_schedule_inputs"),
    )
