"""The UI drives the tracker, through the same code the CLI does (spec 042).

The failure this exists to prevent is two implementations of the same rules.
Every tracker verb lived inline in the CLI's dispatch, so adding routes meant
writing them again, and two copies drift while both suites stay green because
each covers its own.

So the operations moved into `harrier.tracker.actions` and both callers reach
them. `test_the_cli_and_the_api_call_the_same_function` is what holds that:
it patches the action and drives both paths through it.
"""

# Pyright strict cannot resolve starlette's TestClient request and response
# members, which is why every API test file carries these.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import auth
from fastapi.testclient import TestClient

from harrier.db import connect
from harrier.tracker.actions import STATUS_BY_VERB, TrackerActionError, change_status, rescore
from harrier.tracker.store import add_job, get_job
from harrier_api.app import create_app
from harrier_cli.main import main


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    return tmp_path


@pytest.fixture
def job_id(env: Path) -> int:
    conn = connect()
    identifier = add_job(
        conn,
        {
            "company": "Northwind Labs",
            "title": "Senior Frontend Engineer",
            "url": "https://boards.example.com/northwind/1",
            "source": "greenhouse",
            "location": "Remote, Europe",
        },
    )
    conn.close()
    return identifier


@pytest.fixture
def described(job_id: int) -> None:
    """The description the import would have cached.

    `rescore` refuses a job without one (spec 033), so a test about the
    pairing has to give it something to score or it is testing the refusal
    instead.
    """
    from harrier.screening.descriptions import save_description_cache
    from harrier.tracker.store import get_job

    conn = connect()
    save_description_cache(
        get_job(conn, job_id)["url"],
        "Remote across Europe. TypeScript and React, testing and ownership.",
    )
    conn.close()


@pytest.fixture
def client(env: Path) -> TestClient:
    return TestClient(create_app())


# --- one implementation, two callers -----------------------------------------


def test_the_cli_and_the_api_call_the_same_function(job_id: int, client: TestClient) -> None:
    """The spec's central claim, and the reason the actions module exists.

    Both paths are driven through a patched action. If either grew its own
    copy of the rules, its call would not arrive here.
    """
    real_row = get_job(connect(), job_id)

    with patch("harrier.tracker.actions.change_status") as action:
        action.return_value = real_row
        main(["shortlist", str(job_id)])
    cli_call = action.call_args

    with patch("harrier.tracker.actions.change_status") as action:
        action.return_value = real_row
        client.post(f"/tracker/{job_id}/status", json={"verb": "shortlist"}, headers=auth())
    api_call = action.call_args

    assert cli_call is not None, "the CLI did not reach the shared action"
    assert api_call is not None, "the API did not reach the shared action"
    # Same verb, same selector. The connection differs because each opens its
    # own, which is the one argument that legitimately does.
    assert cli_call.args[1:] == api_call.args[1:]


def test_rescore_goes_through_the_same_function(
    job_id: int, client: TestClient, described: None
) -> None:
    with patch("harrier.tracker.actions.rescore") as action:
        action.return_value = rescore(connect(), str(job_id))
        main(["reevaluate", str(job_id)])
        cli_called = action.called

    with patch("harrier.tracker.actions.rescore") as action:
        action.return_value = rescore(connect(), str(job_id))
        client.post(f"/tracker/{job_id}/rescore", headers=auth())
        api_called = action.called

    assert cli_called and api_called


def test_a_job_with_no_stored_description_is_refused_on_both_sides(
    job_id: int, client: TestClient
) -> None:
    """Spec 033's refusal, now reached through spec 042's shared function.

    Rescoring without the description the import had would score a job
    against strictly less input and then overwrite the real number. Both
    callers refuse, and the words are the domain's own on both sides, which
    is the property this pairing exists to hold.
    """
    assert main(["reevaluate", str(job_id)]) == 2

    response = client.post(f"/tracker/{job_id}/rescore", headers=auth())
    assert response.status_code == 409
    assert "no stored description" in response.json()["detail"]


def test_the_browser_cannot_invent_a_transition_the_cli_lacks() -> None:
    """One mapping, so a sixth status cannot appear on one side only."""
    assert set(STATUS_BY_VERB) == {"shortlist", "track", "applied", "interviewing", "reject"}


# --- every transition, over HTTP ---------------------------------------------


@pytest.mark.parametrize(("verb", "status"), sorted(STATUS_BY_VERB.items()))
def test_every_transition_has_a_route(
    job_id: int, client: TestClient, verb: str, status: str
) -> None:
    body = {"verb": verb}
    if verb == "reject":
        body["reason"] = "wrong stack"
    response = client.post(f"/tracker/{job_id}/status", json=body, headers=auth())
    assert response.status_code == 200, response.text
    assert response.json()["status"] == status


def test_an_unknown_verb_is_refused(job_id: int, client: TestClient) -> None:
    response = client.post(f"/tracker/{job_id}/status", json={"verb": "promote"}, headers=auth())
    assert response.status_code == 409
    assert "unknown tracker verb" in response.json()["detail"]


def test_a_selector_matching_nothing_is_a_404(client: TestClient) -> None:
    response = client.post(
        "/tracker/no-such-job/status", json={"verb": "shortlist"}, headers=auth()
    )
    assert response.status_code == 404


def test_a_reason_on_a_non_rejection_is_refused(job_id: int, client: TestClient) -> None:
    """The store only stamps a reason against a rejection, so passing one
    anywhere else would be silently dropped. Named rather than absorbed."""
    response = client.post(
        f"/tracker/{job_id}/status",
        json={"verb": "shortlist", "reason": "because"},
        headers=auth(),
    )
    assert response.status_code == 409
    assert "only recorded on a rejection" in response.json()["detail"]


def test_the_same_refusal_reaches_the_cli(env: Path, job_id: int) -> None:
    """The same message on both sides, which is what the shared action buys."""
    with pytest.raises(TrackerActionError, match="only recorded on a rejection"):
        change_status(connect(), str(job_id), "shortlist", reason="because")


# --- the transitions require the token ---------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("post", "/tracker/1/status", {"verb": "shortlist"}),
        ("post", "/tracker/1/rescore", None),
        ("post", "/tracker", {"company": "A", "title": "T"}),
    ],
)
def test_a_tracker_write_without_the_token_is_refused(
    client: TestClient, method: str, path: str, payload: dict[str, object] | None
) -> None:
    """Spec 042 declares spec 035 a hard dependency, and this is why: these
    routes reach destructive writes from any page open in the browser."""
    call = getattr(client, method)
    response = call(path, json=payload) if payload is not None else call(path)
    assert response.status_code == 403


def test_the_queue_is_readable_without_the_token(client: TestClient) -> None:
    assert client.get("/tracker/queue").status_code == 200
    assert client.get("/tracker/counts").status_code == 200


# --- adding by hand -----------------------------------------------------------


def test_adding_a_job_scores_and_returns_it(client: TestClient) -> None:
    response = client.post(
        "/tracker",
        json={
            "company": "Northwind Labs",
            "title": "Senior Frontend Engineer",
            "url": "https://boards.example.com/northwind/2",
            "location": "Remote, Europe",
        },
        headers=auth(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "added"
    assert body["job"]["company"] == "Northwind Labs"


def test_adding_the_same_job_twice_reports_the_duplicate(client: TestClient) -> None:
    """A refusal the operator asked for, carrying the row it clashed with,
    rather than a status code the UI would translate back into words."""
    payload = {
        "company": "Northwind Labs",
        "title": "Senior Frontend Engineer",
        "url": "https://boards.example.com/northwind/3",
        "location": "Remote, Europe",
    }
    client.post("/tracker", json=payload, headers=auth())
    second = client.post("/tracker", json=payload, headers=auth())
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert second.json()["job"] is not None


def test_adding_without_a_title_is_refused(client: TestClient) -> None:
    response = client.post("/tracker", json={"company": "A", "title": ""}, headers=auth())
    assert response.json()["status"] == "invalid"


# --- the queue orderings ------------------------------------------------------


def test_the_queue_matches_the_cli_ordering(env: Path, client: TestClient) -> None:
    """`next` over HTTP is the same ranking the command line prints."""
    from harrier.tracker.actions import next_up

    conn = connect()
    for index in range(3):
        add_job(
            conn,
            {
                "company": f"Company {index}",
                "title": "Senior Frontend Engineer",
                "url": f"https://boards.example.com/example/{index}",
                "source": "greenhouse",
                "location": "Remote, Europe",
            },
        )
    expected = [row["id"] for row in next_up(conn)]
    conn.close()

    over_http = [str(row["id"]) for row in client.get("/tracker/queue").json()]
    assert over_http == expected


def test_the_undecided_queue_is_narrower(env: Path, job_id: int, client: TestClient) -> None:
    """`review` answers what still needs a decision, which excludes applied."""
    client.post(f"/tracker/{job_id}/status", json={"verb": "applied"}, headers=auth())
    assert client.get("/tracker/queue", params={"undecided": True}).json() == []
    assert client.get("/tracker/queue").json() != []


def test_counts_cover_every_status(env: Path, job_id: int, client: TestClient) -> None:
    counts = client.get("/tracker/counts").json()
    assert counts["prospect"] == 1
    assert set(counts) >= {"prospect", "shortlisted", "applied", "rejected"}


def test_a_change_over_http_is_visible_to_the_cli(
    env: Path, job_id: int, client: TestClient
) -> None:
    """One tracker, not two. The browser and the command line are looking at
    the same rows."""
    # `shortlisted` is a status, not a verb, so this is refused and changes
    # nothing. It used to sit here unasserted, which meant the assertion below
    # passed on the next line alone (review finding on PR #41). Kept as a
    # refusal worth covering rather than deleted.
    refused = client.post(f"/tracker/{job_id}/status", json={"verb": "shortlisted"}, headers=auth())
    assert refused.status_code == 409
    client.post(f"/tracker/{job_id}/status", json={"verb": "shortlist"}, headers=auth())
    conn: sqlite3.Connection = connect()
    assert get_job(conn, job_id)["status"] == "shortlisted"
    conn.close()


def test_a_duplicate_without_a_url_still_carries_the_row_it_clashed_with(
    env: Path, client: TestClient
) -> None:
    """Duplicates are matched by company and title as well as by URL, so the
    answer has to find the row the same way (review finding on PR #41)."""
    payload = {"company": "Alder Works", "title": "Senior Frontend Engineer"}
    first = client.post("/tracker", json=payload, headers=auth())
    assert first.json()["status"] == "added"

    second = client.post("/tracker", json=payload, headers=auth())
    assert second.json()["status"] == "duplicate"
    assert second.json()["job"] is not None
    assert second.json()["job"]["company"] == "Alder Works"
