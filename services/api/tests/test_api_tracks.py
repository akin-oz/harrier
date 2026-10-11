"""The browser works in a chosen search track, and manages tracks (spec 094).

Every database is built under `tmp_path`; every track and row is synthetic
(spec 060, ADR-008).
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

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
from harrier.connection import Connection
from harrier.db import connect
from harrier.tracker import add_job
from harrier.tracks import (
    NON_DEFAULT_OPERATIONS,
    archive_track,
    default_scope,
    operation_refusal,
    resolve_scope,
)
from harrier_api.app import create_app
from harrier_api.deps import (
    ARCHIVED_CAUSE,
    CAUSE_JOIN,
    NOT_AVAILABLE_CAUSE,
    UNKNOWN_TRACK_CAUSE,
    VERB_IN_BODY,
    get_conn,
    get_scope,
)
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

    def tracing_conn() -> Iterator[Connection]:
        for conn in real_get_conn():
            conn.sqlite_driver.set_trace_callback(statements.append)
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


def test_every_track_refusal_is_in_the_contract() -> None:
    """What a route can answer is what the contract declares, judged on the
    document `packages/contract` is generated from (spec 094). Every route
    that takes `track` can answer 404 for an unknown slug, and a 409 is
    declared with the same body wherever one is; adding a track never looks
    one up, so it has no 404."""
    spec = create_app().openapi()
    error_out = {"$ref": "#/components/schemas/ErrorOut"}
    scoped = scoped_routes()
    assert scoped, "the walk found no scoped routes"
    for method, path, _, _ in scoped:
        responses = spec["paths"][path][method.lower()]["responses"]
        for status in ("404", "409"):
            if status == "409" and status not in responses:
                continue
            schema = responses.get(status, {}).get("content", {}).get("application/json", {})
            assert schema.get("schema") == error_out, (method, path, status)
    tracks = spec["paths"]
    assert "404" not in tracks["/tracks"]["post"]["responses"]
    assert {"404", "409"} <= set(tracks["/tracks/{slug}/archive"]["post"]["responses"])
    # A route with no track never claims the track refusals.
    assert "404" not in tracks["/tracks"]["get"]["responses"]
    assert "409" not in tracks["/health"]["get"]["responses"]


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
    # `reconsider` works on an academic track's own seen state (spec 097),
    # and its route passes the track to the CLI as `--track` (spec 050).
    # History reads any track; discovery declares its operation and refuses
    # a non-default track from the browser itself (spec 095).
    # `tailor`, `cover-letter` and `answers` run on an academic track from
    # the command line (spec 101); their routes refuse one, as discovery's
    # does (test_the_apply_routes_refuse_another_track).
    assert allowed == {
        "list",
        "next",
        "counts",
        "add",
        "reconsider",
        "events show",
        "discover",
        "tailor",
        "cover-letter",
        "answers",
        # The selected track's jobs.csv download (spec 096).
        "export",
    }


def test_the_apply_routes_refuse_another_track(two_tracks: Path, client: TestClient) -> None:
    """The CLI writes an academic track's documents (spec 101); the browser
    routes refuse a non-default track before anything is staged, as
    discovery's does (spec 095)."""
    conn = connect()
    try:
        job_id = conn.execute(
            "SELECT id FROM jobs WHERE track_id = (SELECT id FROM tracks WHERE slug = ?)", (SLUG,)
        ).fetchone()[0]
    finally:
        conn.close()
    for path, body, operation in (
        ("resume", {"jd_text": "A posting."}, "tailor"),
        ("cover-letter", {"notes": "Some notes."}, "cover-letter"),
        ("answers", {"questions": "Why us?"}, "answers"),
    ):
        response = client.post(
            f"/apply/{job_id}/{path}", params={"track": SLUG}, json=body, headers=auth()
        )
        assert response.status_code == 409, (path, response.text)
        assert f"{operation} is not available on track {SLUG}" in response.text


def test_every_409_a_route_can_answer_is_in_the_contract() -> None:
    """Two causes of one status are both described. Unpacking one response
    dict over another kept only the last: the apply routes lost their
    default-track refusal and discovery lost its own (review of PR #224)."""
    paths = create_app().openapi()["paths"]

    def conflict(path: str, method: str) -> str:
        return str(paths[path][method]["responses"]["409"]["description"])

    track = NOT_AVAILABLE_CAUSE
    active = "a run of this kind is active"
    # The three document operations run on any track (spec 101), so their
    # routes refuse another track themselves; only `tailor` writes a row,
    # so only it can meet an archived track (review of PR #239).
    for route, archived in (("resume", True), ("cover-letter", False), ("answers", False)):
        text = conflict(f"/apply/{{selector}}/{route}", "post")
        assert active in text and track not in text, (route, text)
        assert (ARCHIVED_CAUSE in text) is archived, (route, text)
        assert "application documents from the browser are the default track's" in text
    evaluate = conflict("/apply/{selector}/evaluate", "post")
    assert track in evaluate and active in evaluate
    assert "default track's" not in evaluate
    artifacts = conflict("/apply/{selector}/artifacts", "get")
    assert artifacts == NOT_AVAILABLE_CAUSE
    discover = conflict("/ops/discover", "post")
    assert "discovery from the browser is the default track's" in discover
    assert ARCHIVED_CAUSE in discover and active in discover


def _track_causes(responses: dict[str, Any], status: str) -> set[str]:
    described = str(responses.get(status, {}).get("description", ""))
    track = {UNKNOWN_TRACK_CAUSE, NOT_AVAILABLE_CAUSE, ARCHIVED_CAUSE}
    return {cause for cause in described.split(CAUSE_JOIN) if cause in track}


def test_every_scoped_route_declares_the_track_refusal(two_tracks: Path) -> None:
    """The route class joins the track refusals with a route's own causes
    rather than letting either replace the other, and claims a 409 cause
    only when `operation_refusal` can give it. The route text hid the 409 on
    about twenty routes (review of PR #234), and the unknown track's 404 on
    every route that names a job; five reads claimed a 409 they never send
    (review of PR #239)."""
    conn = connect()
    try:
        archive_track(conn, SLUG)
        archived = resolve_scope(conn, SLUG)
    finally:
        conn.close()
    assert archived.track.archived
    paths = create_app().openapi()["paths"]
    claims: list[tuple[str, str, set[str]]] = []
    for method, path, operation, _ in scoped_routes():
        responses = paths[path][method.lower()]["responses"]
        assert _track_causes(responses, "404") == {UNKNOWN_TRACK_CAUSE}, (method, path)
        if operation == VERB_IN_BODY:
            # Any string: an unknown verb is not available, a known one is a write.
            expected = {NOT_AVAILABLE_CAUSE, ARCHIVED_CAUSE}
        else:
            # An archived track that is not the default meets both rules, the
            # first that applies answering, as `scope_for` does.
            refusal = operation_refusal(archived, operation)
            if refusal is None:
                expected = set()
            elif refusal == f"track {SLUG} is archived":
                expected = {ARCHIVED_CAUSE}
            else:
                assert refusal == f"{operation} is not available on track {SLUG}"
                expected = {NOT_AVAILABLE_CAUSE}
        claims.append((method, path, _track_causes(responses, "409")))
        assert claims[-1][2] == expected, (method, path, operation)
    assert any(causes == set() for _, _, causes in claims)
    # The reads every track runs claim no track 409 at all.
    for path in (
        "/jobs",
        "/tracker/queue",
        "/tracker/counts",
        "/tracker/{selector}/events",
        "/ops/export/jobs.csv",
    ):
        assert "409" not in paths[path]["get"]["responses"], path
    # A route's own causes stay beside the track's.
    draft = paths["/outreach/{selector}/draft"]["post"]["responses"]["409"]["description"]
    assert "the domain refused the change" in draft
    assert "a run of this kind is active" in draft
    events = paths["/tracker/{selector}/events"]["get"]["responses"]["404"]["description"]
    assert events.startswith("the selector named no job, or more than one")


def _second_track_job_id() -> int:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id FROM jobs WHERE track_id = (SELECT id FROM tracks WHERE slug = ?)"
            " ORDER BY id",
            (SLUG,),
        ).fetchone()
    finally:
        conn.close()
    return int(row[0])


def _jobs_snapshot() -> list[tuple[Any, ...]]:
    conn = connect()
    try:
        return [tuple(r) for r in conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()]
    finally:
        conn.close()


def test_a_read_on_an_archived_track_answers_what_its_contract_says(
    two_tracks: Path, client: TestClient
) -> None:
    """Every scoped read, provoked on an archived track that is not the
    default, with a job on that track so no route's own 404 hides the
    answer. Its status is one its contract declares; it is refused by the
    track exactly when its contract names a track 409; and a read that
    declares no 409 at all answers 200 (reviews of PRs #239 and #255).
    Reads only, so nothing starts a run."""
    job_id = _second_track_job_id()
    assert client.post(f"/tracks/{SLUG}/archive", headers=auth()).status_code == 200
    paths = create_app().openapi()["paths"]
    track_details = (f"is not available on track {SLUG}", f"track {SLUG} is archived")
    reads = [path for method, path, _, _ in scoped_routes() if method == "GET"]
    assert len(reads) >= 5
    answered_200: list[str] = []
    for path in reads:
        responses = paths[path]["get"]["responses"]
        url = path.replace("{selector}", str(job_id)).replace("{kind}", "resume")
        response = client.get(url, params={"track": SLUG}, headers=auth())
        assert str(response.status_code) in responses, (path, response.status_code)
        detail = str(response.json().get("detail", "")) if response.status_code == 409 else ""
        refused = any(text in detail for text in track_details)
        assert refused == bool(_track_causes(responses, "409")), (path, response.status_code)
        if "409" not in responses:
            assert response.status_code == 200, (path, response.text)
            answered_200.append(path)
    assert "/tracker/{selector}/events" in answered_200


def test_an_unknown_track_is_a_404_on_every_scoped_route(
    two_tracks: Path, client: TestClient
) -> None:
    """Writes included: the track is resolved before the body is read, so
    every scoped route answers 404 naming the slug, as its contract says,
    and changes no row (review of PR #255)."""
    paths = create_app().openapi()["paths"]
    before = _jobs_snapshot()
    scoped = scoped_routes()
    assert any(method != "GET" for method, _, _, _ in scoped)
    for method, path, _, _ in scoped:
        url = path.replace("{selector}", "1").replace("{kind}", "resume")
        params = {"track": "no-such-track"}
        response = client.request(method, url, params=params, headers=auth(), json={})
        assert response.status_code == 404, (method, path, response.text)
        assert "no-such-track" in response.json()["detail"], (method, path)
        responses = paths[path][method.lower()]["responses"]
        assert UNKNOWN_TRACK_CAUSE in _track_causes(responses, "404"), (method, path)
    # A verb the status route would accept still moves nothing.
    moved = client.post(
        "/tracker/1/status",
        params={"track": "no-such-track"},
        json={"verb": "shortlist"},
        headers=auth(),
    )
    assert moved.status_code == 404
    assert _jobs_snapshot() == before


def test_the_status_route_checks_its_verb_against_the_track(
    two_tracks: Path, client: TestClient
) -> None:
    """The status route's verb is in its body, so the route checks it with
    the same rule `scope_for` applies: an unknown verb is not available on a
    non-default track, and a known one is a write an archived track refuses
    (review of PR #255)."""
    job_id = _second_track_job_id()
    unknown = client.post(
        f"/tracker/{job_id}/status",
        params={"track": SLUG},
        json={"verb": "no-such-verb"},
        headers=auth(),
    )
    assert unknown.status_code == 409
    assert unknown.json()["detail"] == f"no-such-verb is not available on track {SLUG}"
    assert client.post(f"/tracks/{SLUG}/archive", headers=auth()).status_code == 200
    archived = client.post(
        f"/tracker/{job_id}/status",
        params={"track": SLUG},
        json={"verb": "shortlist"},
        headers=auth(),
    )
    assert archived.status_code == 409
    assert archived.json()["detail"] == f"track {SLUG} is archived"


def test_merge_responses_keeps_every_whole_cause() -> None:
    from harrier_api.deps import merge_responses

    kept = merge_responses(
        {409: {"description": "archived track"}}, {409: {"description": "archived"}}
    )
    assert kept[409]["description"] == "archived track; or archived"
    unnamed = merge_responses({409: {"model": "A"}}, {409: {"description": "x"}})
    assert unnamed[409]["description"] == "x"
    with pytest.raises(ValueError, match="two body models"):
        merge_responses({409: {"model": "A"}}, {409: {"model": "B"}})
    # A joined list merged again adds nothing it already has (review of PR #239).
    joined: dict[int | str, dict[str, Any]] = {409: {"description": "a; or b"}}
    assert merge_responses(joined, joined)[409]["description"] == "a; or b"
    assert merge_responses(joined, {409: {"description": "b; or c"}})[409]["description"] == (
        "a; or b; or c"
    )


def test_merge_responses_joins_one_status_and_keeps_the_rest() -> None:
    from harrier_api.deps import merge_responses

    merged = merge_responses(
        {404: {"description": "no job"}, 409: {"description": "first", "model": "A"}},
        {409: {"description": "second"}},
        {409: {"description": "first"}},
    )
    assert merged[404] == {"description": "no job"}
    assert merged[409] == {"description": "first; or second", "model": "A"}


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
