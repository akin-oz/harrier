"""The browser works in a chosen search track, and manages tracks (spec 094).

Every database is built under `tmp_path`; every track and row is synthetic
(spec 060, ADR-008).
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from conftest import auth
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import harrier.tracks as tracks_module
import harrier_cli.main as cli_module
from harrier.db import connect
from harrier.tracker import add_job
from harrier.tracks import NON_DEFAULT_OPERATIONS, default_scope, resolve_scope
from harrier_api.app import create_app
from harrier_api.deps import VERB_IN_BODY, get_conn, get_scope
from harrier_cli.main import main

SLUG = "second-search"

JOBS_FIRST = [
    {
        "company": "Example Co",
        "title": "Senior Frontend Engineer",
        "url": "https://boards.example.com/a/1",
    },
]
JOBS_SECOND = [
    {
        "company": "Example Lab",
        "title": "Research Engineer",
        "url": "https://calls.example.com/1",
        "deadline": "2099-03-01",
    },
    {
        "company": "Long Gone Lab",
        "title": "Platform Engineer",
        "url": "https://calls.example.com/2",
        "deadline": "2020-01-02",
    },
]


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "data"
    monkeypatch.setenv("HARRIER_DATA_DIR", str(directory))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    return directory


@pytest.fixture()
def two_tracks(data_dir: Path) -> Iterator[Path]:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO tracks (id, slug, kind, label) VALUES (2, ?, 'academic', 'Second search')",
            (SLUG,),
        )
        conn.commit()
        first, second = default_scope(conn), resolve_scope(conn, SLUG)
        for row in JOBS_FIRST:
            add_job(conn, row, scope=first)
        for row in JOBS_SECOND:
            add_job(conn, row, scope=second)
    finally:
        conn.close()
    yield data_dir


@pytest.fixture()
def client(data_dir: Path) -> TestClient:
    return TestClient(create_app())


def api_routes(routes: list[Any]) -> Iterator[APIRoute]:
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from api_routes(original.routes)


def calls(dependant: Any) -> list[Any]:
    found = [dependant.call] if dependant.call is not None else []
    for child in dependant.dependencies:
        found.extend(calls(child))
    return found


def scoped_routes() -> list[tuple[str, str, str, APIRoute]]:
    """Every (method, path, operation, route) whose scope comes from `scope_for`."""
    found: list[tuple[str, str, str, APIRoute]] = []
    for route in api_routes(list(create_app().routes)):
        for call in calls(route.dependant):
            operation = getattr(call, "track_operation", None)
            if operation is not None:
                for method in sorted(route.methods or set()):
                    found.append((method, route.path, operation, route))
    return found


def ids(response: Any) -> set[str]:
    return {str(job["company"]) for job in response.json()}


# --- naming a track ----------------------------------------------------------------


def test_a_request_names_its_track_by_query_parameter(two_tracks: Path, client: TestClient) -> None:
    assert ids(client.get("/jobs")) == {"Example Co"}
    assert ids(client.get("/jobs", params={"track": SLUG})) == {"Example Lab", "Long Gone Lab"}
    assert ids(client.get("/jobs", params={"track": "job"})) == {"Example Co"}
    counts = client.get("/tracker/counts", params={"track": SLUG}).json()
    assert counts["prospect"] == len(JOBS_SECOND)


def test_an_unknown_track_is_404_and_a_malformed_slug_is_422(
    two_tracks: Path, client: TestClient
) -> None:
    unknown = client.get("/jobs", params={"track": "no-such-track"})
    assert unknown.status_code == 404
    assert "no-such-track" in unknown.json()["detail"]
    for bad in ("Not A Slug", "-x", "a" * 33, "job_search"):
        assert client.get("/jobs", params={"track": bad}).status_code == 422, bad


# --- one allowlist ---------------------------------------------------------------------


def _request(client: TestClient, method: str, path: str) -> Any:
    url = path.replace("{selector}", "1").replace("{kind}", "resume")
    params = {"track": SLUG}
    if method == "GET":
        return client.get(url, params=params, headers=auth())
    return client.request(method, url, params=params, headers=auth(), json={})


def test_routes_outside_the_allowlist_refuse_a_non_default_track(
    two_tracks: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    statements: list[str] = []
    real_get_conn = get_conn

    def tracing_conn() -> Iterator[sqlite3.Connection]:
        for conn in real_get_conn():
            conn.set_trace_callback(statements.append)
            yield conn

    client.app.dependency_overrides[get_conn] = tracing_conn  # type: ignore[attr-defined]
    refused = [
        (method, path, operation)
        for method, path, operation, _ in scoped_routes()
        if operation not in NON_DEFAULT_OPERATIONS and operation != VERB_IN_BODY
    ]
    assert refused, "the walk found no refused routes"
    for method, path, operation in refused:
        statements.clear()
        response = _request(client, method, path)
        assert response.status_code == 409, (method, path, response.text)
        assert response.json()["detail"] == f"{operation} is not available on track {SLUG}"
        assert not any("FROM jobs" in s for s in statements), (method, path)
    # The status route checks the verb in its body against the same list: every
    # browser verb is on it, so each moves a row on the second track.
    conn = connect()
    try:
        job_id = conn.execute("SELECT id FROM jobs WHERE track_id = 2 ORDER BY id").fetchone()[0]
    finally:
        conn.close()
    for verb in ("shortlist", "track", "applied", "interviewing", "reject"):
        assert verb in NON_DEFAULT_OPERATIONS
    moved = client.post(
        f"/tracker/{job_id}/status",
        params={"track": SLUG},
        json={"verb": "shortlist"},
        headers=auth(),
    )
    assert moved.status_code == 200, moved.text
    client.app.dependency_overrides.clear()  # type: ignore[attr-defined]


def test_every_scoped_route_declares_its_operation() -> None:
    """A route that reads tracker rows declares its operation through
    `scope_for`, which adds the `track` parameter. Only the capture routes use
    the bare default scope, and they take no `track` parameter."""
    app = create_app()
    schema = app.openapi()
    bare: list[str] = []
    for route in api_routes(list(app.routes)):
        found = calls(route.dependant)
        declared = [c for c in found if getattr(c, "track_operation", None) is not None]
        if get_scope in found:
            bare.append(route.path)
        for method in route.methods or set():
            parameters = schema["paths"][route.path][method.lower()].get("parameters", [])
            names = {p["name"] for p in parameters if p.get("in") == "query"}
            if declared:
                assert "track" in names, (method, route.path)
            if get_scope in found:
                assert "track" not in names, (method, route.path)
    assert sorted(set(bare)) == ["/capture/add", "/capture/add-form"]
    assert len(scoped_routes()) >= 24


def test_the_cli_and_the_api_share_one_allowlist() -> None:
    assert cli_module.TRACK_ALLOWLIST is tracks_module.NON_DEFAULT_OPERATIONS
    assert cli_module.TRACK_WRITES is tracks_module.WRITE_OPERATIONS
    allowed = {op for _, _, op, _ in scoped_routes() if op in NON_DEFAULT_OPERATIONS}
    assert allowed == {"list", "next", "counts", "add"}


def test_an_archived_track_refuses_writes_over_http(two_tracks: Path, client: TestClient) -> None:
    archived = client.post(f"/tracks/{SLUG}/archive", headers=auth())
    assert archived.status_code == 200 and archived.json()["archived"] is True
    response = client.post(
        "/tracker",
        params={"track": SLUG},
        json={"company": "Later Lab", "title": "Engineer"},
        headers=auth(),
    )
    assert response.status_code == 409
    assert response.json()["detail"] == f"track {SLUG} is archived"
    assert ids(client.get("/jobs", params={"track": SLUG})) == {"Example Lab", "Long Gone Lab"}


# --- the tracks routes -------------------------------------------------------------------


def test_the_tracks_routes_list_add_and_archive(data_dir: Path, client: TestClient) -> None:
    listed = client.get("/tracks").json()
    assert [(t["slug"], t["is_default"], t["archived"]) for t in listed] == [("job", True, False)]
    assert listed[0]["status_labels"]["prospect"] == "prospect"

    body = {"slug": SLUG, "kind": "academic", "label": "Second search"}
    assert client.post("/tracks", json=body).status_code == 403
    created = client.post("/tracks", json=body, headers=auth())
    assert created.status_code == 201
    assert created.json()["status_labels"]["applied"] == "submitted"
    assert client.post("/tracks", json=body, headers=auth()).status_code == 409
    industry = {"slug": "more-jobs", "kind": "industry", "label": "More"}
    refused = client.post("/tracks", json=industry, headers=auth())
    assert refused.status_code == 409
    # The add form disables the industry option with the words the refusal
    # carries, read from the domain rather than written into the browser.
    kinds = {k["kind"]: k for k in client.get("/tracks/kinds").json()}
    assert kinds["academic"] == {"kind": "academic", "available": True, "reason": ""}
    assert kinds["industry"]["available"] is False
    assert kinds["industry"]["reason"] == refused.json()["detail"]
    for bad in ({**body, "slug": "Bad Slug"}, {**body, "kind": "freelance"}, {**body, "label": ""}):
        assert client.post("/tracks", json=bad, headers=auth()).status_code == 422

    assert client.post(f"/tracks/{SLUG}/archive").status_code == 403
    assert client.post(f"/tracks/{SLUG}/archive", headers=auth()).status_code == 200
    assert client.post(f"/tracks/{SLUG}/archive", headers=auth()).status_code == 409
    assert client.post("/tracks/job/archive", headers=auth()).status_code == 409
    assert client.post("/tracks/no-such-track/archive", headers=auth()).status_code == 404
    assert [t["archived"] for t in client.get("/tracks").json()] == [False, True]


def test_the_cli_and_the_api_manage_tracks_through_the_same_functions(
    data_dir: Path, client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    import harrier_api.tracks_routes as routes_module

    with patch.object(tracks_module, "add_track", wraps=tracks_module.add_track) as cli_side:
        assert main(["tracks", "add", SLUG, "--kind", "academic", "--label", "Second search"]) == 0
    capsys.readouterr()
    with patch.object(routes_module, "add_track", wraps=tracks_module.add_track) as api_side:
        body = {"slug": "third-search", "kind": "academic", "label": "Third search"}
        assert client.post("/tracks", json=body, headers=auth()).status_code == 201
    assert cli_side.call_args.args[1:] == (SLUG, "academic", "Second search")
    assert api_side.call_args.args[1:] == ("third-search", "academic", "Third search")
    assert routes_module.archive_track is tracks_module.archive_track
    assert routes_module.list_tracks is tracks_module.list_tracks


# --- jobs carry their track and deadline -------------------------------------------------


def test_jobs_carry_their_track_and_deadline(two_tracks: Path, client: TestClient) -> None:
    rows = {job["company"]: job for job in client.get("/jobs", params={"track": SLUG}).json()}
    assert rows["Example Lab"]["track"] == SLUG
    assert rows["Example Lab"]["deadline"] == "2099-03-01"
    assert rows["Example Lab"]["deadline_passed"] is False
    assert rows["Long Gone Lab"]["deadline_passed"] is True
    first = client.get("/jobs").json()[0]
    assert first["track"] == "job" and first["deadline"] == "" and first["deadline_passed"] is False

    added = client.post(
        "/tracker",
        params={"track": SLUG},
        json={"company": "Added Lab", "title": "Engineer", "deadline": "2099-05-01"},
        headers=auth(),
    )
    assert added.status_code == 200, added.text
    assert added.json()["job"]["deadline"] == "2099-05-01"
    impossible = client.post(
        "/tracker",
        params={"track": SLUG},
        json={"company": "Bad Lab", "title": "Engineer", "deadline": "2099-02-30"},
        headers=auth(),
    )
    assert impossible.status_code == 422


def test_the_academic_queue_over_http_matches_the_cli(
    two_tracks: Path, client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    over_http = [
        job["company"] for job in client.get("/tracker/queue", params={"track": SLUG}).json()
    ]
    assert main(["--track", SLUG, "next"]) == 0
    out = capsys.readouterr().out
    printed = [
        line.split(". ", 1)[1].split(" - ")[0]
        for line in out.splitlines()
        if ". " in line and " - " in line
    ]
    assert over_http == printed == ["Example Lab", "Long Gone Lab"]
