"""Read-path API behavior (spec 005).

Pyright strict cannot resolve starlette's TestClient request/response types
(they surface as Unknown through httpx private aliases), so the three
unknown-type rules are off for this file only. Everything else stays strict.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

import json
from pathlib import Path
from typing import get_args

import pytest
from fastapi.testclient import TestClient

from harrier.db import connect
from harrier.tracker import STATUSES, add_job, set_status
from harrier_api.app import JobStatus, create_app


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    return TestClient(create_app())


def _seed(tmp_path: Path) -> None:
    conn = connect(tmp_path / "data" / "tracker.db")
    try:
        first = add_job(
            conn,
            {
                "company": "Acme",
                "title": "Senior Frontend Engineer",
                "url": "https://boards.example.com/acme/1",
                "source": "greenhouse",
                "notes": "score=80; external_key=gh:acme:1",
            },
        )
        add_job(
            conn,
            {
                "company": "Beta",
                "title": "Product Engineer",
                "url": "https://jobs.example.eu/beta/2",
                "source": "ashby",
                "notes": "",
            },
        )
        set_status(conn, first, "shortlisted")
    finally:
        conn.close()


def test_status_literal_matches_tracker_statuses() -> None:
    assert set(get_args(JobStatus)) == set(STATUSES)


def test_jobs_empty(client: TestClient) -> None:
    response = client.get("/jobs")
    assert response.status_code == 200
    assert response.json() == []


def test_jobs_seeded_and_filtered(client: TestClient, tmp_path: Path) -> None:
    _seed(tmp_path)
    everything = client.get("/jobs").json()
    assert [job["company"] for job in everything] == ["Acme", "Beta"]
    assert everything[0]["score"] == "80"
    assert everything[0]["external_key"] == "gh:acme:1"

    shortlisted = client.get("/jobs", params={"status": "shortlisted"}).json()
    assert [job["company"] for job in shortlisted] == ["Acme"]
    by_source = client.get("/jobs", params={"source": "ashby"}).json()
    assert [job["company"] for job in by_source] == ["Beta"]


def test_invalid_status_is_422(client: TestClient) -> None:
    response = client.get("/jobs", params={"status": "ghosted"})
    assert response.status_code == 422


def test_health_reports_count(client: TestClient, tmp_path: Path) -> None:
    _seed(tmp_path)
    body = client.get("/health").json()
    assert body["name"] == "harrier"
    assert body["demo"] is False
    assert body["job_count"] == 2


def test_demo_mode_serves_fixture_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARRIER_DEMO", "1")
    with TestClient(create_app()) as demo_client:
        body = demo_client.get("/jobs").json()
        fixture = json.loads(
            (Path(__file__).resolve().parents[3] / "fixtures" / "demo-jobs.json").read_text(
                encoding="utf-8"
            )
        )
        assert len(body) == len(fixture)
        assert {job["company"] for job in body} == {entry["company"] for entry in fixture}
        assert demo_client.get("/health").json()["demo"] is True


def test_concurrent_requests_do_not_trip_the_sqlite_thread_check(
    client: TestClient, tmp_path: Path
) -> None:
    """FastAPI runs a sync dependency and its endpoint on different
    threadpool threads, so a connection made in one is used in the other.
    Under concurrent requests that raised sqlite3.ProgrammingError and the
    route 500'd. It surfaced only once the web app started fetching /health
    and /jobs at the same time (spec 026)."""
    import concurrent.futures

    _seed(tmp_path)

    def hit(path: str) -> int:
        return client.get(path).status_code

    paths = ["/jobs", "/health"] * 12
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(hit, paths))
    assert set(codes) == {200}, codes
