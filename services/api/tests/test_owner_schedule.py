"""The owner sees their schedule (spec 120).

Hosted, `GET /ops/schedule` answers from the signed-in owner's `schedule`
choice and their `schedule_state` rows, read on their own connection.

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
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest
from pg_support import as_owner
from test_hosted_requests import Hosted, Server, copied_server, serving

from harrier.hostedschedule import load_hosted_jobs, next_slot
from harrier.pgstore import postgres_connect
from harrier.schedule import ScheduleConfigError
from harrier.userconfig import ConfigError

if TYPE_CHECKING:
    import psycopg

    PgConnection = psycopg.Connection[Any]

BERLIN = "Europe/Berlin"
# Spec 120's wording, written out rather than imported from the module
# under test, so a change to what the owner reads is a failure here.
HOSTED_SCHEDULED = "Hosted, the scheduler starts each chosen job at its slots in your timezone."
HOSTED_UNSCHEDULED = (
    "You have not chosen a schedule. Nothing runs on a schedule; choose jobs in Settings."
)
HOSTED_PAUSED = "Your schedule is paused: no job is chosen."
HOSTED_DEFINITION_UNREADABLE = (
    "The server's schedule definition could not be read, so nothing runs on a schedule "
    "until it is fixed."
)
OFFERED = [
    {"name": "discovery", "cadence": "daily at 09:00, 13:00, 16:00, 20:00", "needs": []},
    {"name": "academic-discovery", "cadence": "Mondays at 09:30", "needs": ["apify"]},
]
STATE = {
    "last_slot_at": "2026-10-12T18:00:00Z",
    "last_outcome": "failed",
    "last_started_at": "2026-10-12T18:01:00Z",
    "last_finished_at": "2026-10-12T18:09:00Z",
    "last_ok_at": "2026-10-12T14:08:00Z",
}


@pytest.fixture(name="server")
def server_fixture(postgres_template: str) -> Iterator[Server]:
    yield from copied_server(postgres_template)


@pytest.fixture(name="hosted")
def hosted_fixture(server: Server, monkeypatch: pytest.MonkeyPatch) -> Iterator[Hosted]:
    with serving(monkeypatch, server.login_role().url) as built:
        yield built


def write_state(url: str, owner: str, job: str, state: dict[str, str]) -> None:
    with closing(postgres_connect(url)) as conn, as_owner(conn, owner) as policed:
        policed.execute(
            "INSERT INTO schedule_state (job, last_slot_at, last_started_at, last_finished_at, "
            "last_outcome, last_ok_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (
                job,
                state["last_slot_at"],
                state["last_started_at"],
                state["last_finished_at"],
                state["last_outcome"],
                state["last_ok_at"],
            ),
        )


def write_raw_schedule(url: str, owner: str, value: str) -> None:
    """A stored row the store would refuse today, written past its rules,
    as a row written before a rule changed would be."""
    with closing(postgres_connect(url)) as conn, as_owner(conn, owner) as policed:
        policed.execute(
            "INSERT INTO user_config (kind, value, updated_at) "
            "VALUES ('schedule', %s, '2026-10-12 08:00:00')",
            (value,),
        )


def choose(hosted: Hosted, owner: str, value: object) -> None:
    put = hosted.client().put(
        "/config/schedule", json={"value": value}, headers=hosted.headers(owner)
    )
    assert put.status_code == 200, put.text


def read(hosted: Hosted, owner: str) -> dict[str, Any]:
    response = hosted.client().get("/ops/schedule", headers=hosted.headers(owner))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_hosted_schedule_lists_the_owners_chosen_jobs_and_their_last_run(
    server: Server, hosted: Hosted
) -> None:
    owner = server.owner()
    choose(hosted, owner, {"timezone": BERLIN, "jobs": ["academic-discovery", "discovery"]})
    write_state(server.url, owner, "discovery", STATE)
    before = datetime.now(UTC)

    body = read(hosted, owner)

    assert (body["timezone"], body["host_command"], body["error"]) == (BERLIN, None, None)
    assert body["installed_state"] == HOSTED_SCHEDULED
    assert body["offered"] == OFFERED
    # In HOSTED_JOBS order, whatever order the owner chose them in.
    assert [job["name"] for job in body["jobs"]] == ["discovery", "academic-discovery"]
    discovery, academic = body["jobs"]
    assert discovery["records"] == []
    assert discovery["cadence"] == OFFERED[0]["cadence"]
    assert {key: discovery["hosted"][key] for key in STATE} == STATE
    # No state row yet: every recorded field is null.
    assert {key: academic["hosted"][key] for key in STATE} == dict.fromkeys(STATE)

    jobs = load_hosted_jobs()
    after = datetime.now(UTC)
    for job in body["jobs"]:
        slot = datetime.fromisoformat(job["hosted"]["next_slot_at"].replace("Z", "+00:00"))
        expected = {
            next_slot(jobs[job["name"]], ZoneInfo(BERLIN), moment) for moment in (before, after)
        }
        assert slot in expected, (job["name"], slot, expected)
        assert slot > before


def test_hosted_schedule_names_an_unscheduled_paused_or_unreadable_choice(
    server: Server, hosted: Hosted
) -> None:
    unscheduled = server.owner()
    body = read(hosted, unscheduled)
    assert (body["jobs"], body["timezone"], body["error"]) == ([], None, None)
    assert body["installed_state"] == HOSTED_UNSCHEDULED
    assert body["offered"] == OFFERED

    paused = server.owner()
    choose(hosted, paused, {"timezone": BERLIN, "jobs": []})
    body = read(hosted, paused)
    assert (body["jobs"], body["timezone"], body["error"]) == ([], BERLIN, None)
    assert body["installed_state"] == HOSTED_PAUSED

    unreadable = server.owner()
    write_raw_schedule(
        server.url, unreadable, json.dumps({"timezone": "Mars/Olympus_Mons", "jobs": []})
    )
    body = read(hosted, unreadable)
    assert body["jobs"] == [] and body["timezone"] is None
    assert body["error"] == "schedule.timezone is not a zone this server can load"
    assert body["offered"] == OFFERED
    assert body["installed_state"] == HOSTED_UNSCHEDULED


def test_hosted_schedule_offers_nothing_when_the_definition_cannot_be_read(
    server: Server, hosted: Hosted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A committed definition that fails to load is the server's fault: the
    answer offers no job (`offered` null, so the page offers no save) and
    says so, rather than reading as an owner who chose nothing."""
    owner = server.owner()
    choose(hosted, owner, {"timezone": BERLIN, "jobs": ["discovery"]})

    def unreadable(path: object = None) -> object:
        raise ScheduleConfigError("config/schedule.json: no jobs")

    monkeypatch.setattr("harrier.hostedschedule.load_hosted_jobs", unreadable)
    body = read(hosted, owner)
    assert (body["jobs"], body["offered"], body["timezone"]) == ([], None, None)
    assert body["error"] == "config/schedule.json: no jobs"
    assert body["installed_state"] == HOSTED_DEFINITION_UNREADABLE


def test_hosted_schedule_scrubs_a_refusal_before_answering(
    server: Server, hosted: Hosted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal's text may quote what it refused, so it is scrubbed before
    it leaves the process (spec 133), as the local answer's is."""
    owner = server.owner()
    secret = "synthetic-secret-value"

    def refusing(conn: object, kind: str) -> object:
        raise ConfigError(f"stored schedule configuration is not valid JSON: token={secret}")

    monkeypatch.setattr("harrier.userconfig.get_config", refusing)
    body = read(hosted, owner)
    assert body["error"] == ("stored schedule configuration is not valid JSON: token=REDACTED"), (
        body
    )
    assert secret not in json.dumps(body)


def test_hosted_schedule_reads_only_the_owners_state(server: Server, hosted: Hosted) -> None:
    mine, theirs = server.owner(), server.owner()
    choose(hosted, mine, {"timezone": BERLIN, "jobs": ["discovery"]})
    choose(hosted, theirs, {"timezone": "UTC", "jobs": ["discovery", "academic-discovery"]})
    write_state(server.url, theirs, "discovery", {**STATE, "last_outcome": "ok"})
    write_state(server.url, theirs, "academic-discovery", STATE)

    body = read(hosted, mine)
    assert body["timezone"] == BERLIN
    assert [job["name"] for job in body["jobs"]] == ["discovery"]
    assert body["jobs"][0]["hosted"]["last_outcome"] is None

    write_state(server.url, mine, "discovery", {**STATE, "last_outcome": "running"})
    assert read(hosted, mine)["jobs"][0]["hosted"]["last_outcome"] == "running"
    assert read(hosted, theirs)["jobs"][0]["hosted"]["last_outcome"] == "ok"


# --- the next slot, read as the scheduler reads a slot ---


def test_the_next_slot_is_the_first_after_now_in_the_owner_s_zone() -> None:
    jobs = load_hosted_jobs()
    berlin = ZoneInfo(BERLIN)
    discovery, academic = jobs["discovery"], jobs["academic-discovery"]
    # Friday 16 October 2026, 19:15 in Berlin (UTC+2): the next is 20:00.
    friday = datetime(2026, 10, 16, 17, 15, tzinfo=UTC)
    assert next_slot(discovery, berlin, friday) == datetime(2026, 10, 16, 18, 0, tzinfo=UTC)
    # Exactly on a slot, the next one is the following slot, not this one.
    on_slot = datetime(2026, 10, 16, 18, 0, tzinfo=UTC)
    assert next_slot(discovery, berlin, on_slot) == datetime(2026, 10, 17, 7, 0, tzinfo=UTC)
    # Weekly: Monday 19 October at 09:30 in Berlin.
    assert next_slot(academic, berlin, friday) == datetime(2026, 10, 19, 7, 30, tzinfo=UTC)
    # Just after a weekly slot, the next is a week on, after the clocks go
    # back: Monday 26 October at 09:30 is 08:30 UTC.
    after_weekly = datetime(2026, 10, 19, 7, 31, tzinfo=UTC)
    assert next_slot(academic, berlin, after_weekly) == datetime(2026, 10, 26, 8, 30, tzinfo=UTC)
    # Across the clock change on Sunday 25 October: 09:00 is then UTC+1.
    saturday_night = datetime(2026, 10, 24, 19, 0, tzinfo=UTC)
    assert next_slot(discovery, berlin, saturday_night) == datetime(2026, 10, 25, 8, 0, tzinfo=UTC)
    # A zone far ahead of UTC reads its own calendar day.
    auckland = ZoneInfo("Pacific/Auckland")
    assert next_slot(discovery, auckland, friday) == datetime(2026, 10, 16, 20, 0, tzinfo=UTC)
    soon = next_slot(discovery, berlin, friday)
    assert soon is not None and soon - friday < timedelta(hours=1)
