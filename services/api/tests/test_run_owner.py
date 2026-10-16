"""Hosted runs know their owner (spec 132).

Hosted, a run belongs to the verified `sub` of the request that started it,
and every run route answers only that owner. The apps here are built with
no store, so no run is bound to an owner here (spec 106's hosted runs are
`tests/test_hosted_runs.py`'s); the runs these tests look at are started
the other ways the spec names: by the test, while the process is local, or
from a journal.
Then `HARRIER_AUTH=supabase` makes the process hosted, and the hosted app,
built as `tests/test_hosted_auth.py` builds it, answers with tokens made
here. No test reaches the network or a database server.

Every token and key here is made at run time.
"""

# Pyright strict cannot resolve starlette's TestClient request and response
# members, which is why every API test file carries these.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import asyncio
import functools
import json
import math
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from conftest import auth
from fastapi.testclient import TestClient
from starlette.requests import Request
from test_hosted_auth import (
    API_HOST,
    CONFIG,
    KID,
    Clock,
    KeyServer,
    bearer,
    claims_for,
    mint,
    new_ec_key,
)

from harrier.backup import ARCHIVE_PREFIX, ARCHIVE_SUFFIX
from harrier.db import connect
from harrier.demo import repo_root
from harrier.filestore import LocalFileStore, bind_files
from harrier.tracker.store import add_job
from harrier.tracks import default_scope
from harrier_api.app import create_app, create_hosted_app
from harrier_api.hostedauth import HOSTED_APP_REFUSAL, HostedConfigError
from harrier_api.runmodels import (
    RUN_NOT_HOSTED_DESCRIPTION,
    TOKEN_EXPIRED_LINE,
    OwnedRuns,
    get_runs,
)
from harrier_api.runs import (
    GMAIL_NOT_HOSTED_DETAIL,
    TERMINAL_STATES,
    Run,
    RunManager,
    RunParams,
    run_inputs_dir,
)
from harrier_api.settings_routes import VERIFY_KIND

CONTRACT = repo_root() / "packages" / "contract" / "openapi.json"
NOT_FOUND = {"detail": "run not found"}
SLOW_SCRIPT = "import time; print('starting', flush=True); time.sleep(30)"


def quiet_until(signal: Path) -> str:
    """A run that sends one event, then sends nothing until `signal` exists
    (at most 15 seconds), then one more and succeeds."""
    return (
        "import json, os, time\n"
        'print("::harrier::" + json.dumps({"event": "progress", "step": 1}), flush=True)\n'
        "deadline = time.monotonic() + 15\n"
        f"while not os.path.exists({str(signal)!r}) and time.monotonic() < deadline:\n"
        "    time.sleep(0.05)\n"
        'print("::harrier::" + json.dumps({"event": "progress", "step": 2}), flush=True)\n'
    )


# --- the hosted app, and runs put in it ---


def on_loop[T](client: TestClient, call: Callable[[], Awaitable[T]]) -> T:
    """Run `call` on the loop the app serves from, where the manager's runs
    live. The client must be entered."""
    portal = client.portal
    assert portal is not None, "the client is not entered"
    return portal.call(call)


@dataclass
class Owners:
    app: Any
    client: TestClient
    manager: RunManager
    key: Any
    monkeypatch: pytest.MonkeyPatch

    def as_owner(self, sub: str, **overrides: object) -> dict[str, str]:
        return bearer(mint(self.key, claims_for(sub=sub, **overrides), kid=KID))

    def start_local(self, owner: str, kind: str = "demo", params: RunParams | None = None) -> Run:
        """A run put in the registry while the process is local, as a test
        or a journal in the image would put one (spec 132, Honest
        limitations), then the process is hosted again."""
        self.monkeypatch.delenv("HARRIER_AUTH")
        try:
            return on_loop(
                self.client, functools.partial(self.manager.start, kind, params, owner=owner)
            )
        finally:
            self.monkeypatch.setenv("HARRIER_AUTH", "supabase")

    def run(self, run_id: str) -> Run:
        run = self.manager.get(run_id)
        assert run is not None
        return run

    def stop(self, run_id: str) -> None:
        on_loop(self.client, functools.partial(self.manager.cancel, run_id))
        deadline = time.monotonic() + 10
        while self.run(run_id).state not in TERMINAL_STATES and time.monotonic() < deadline:
            time.sleep(0.05)


def hosted_manager(tmp_path: Path, script: str = SLOW_SCRIPT) -> RunManager:
    return RunManager(
        journal_path=tmp_path / "journal.jsonl",
        # Two kinds, so two runs are active at once rather than one joining.
        kind_commands={kind: [sys.executable, "-c", script] for kind in ("demo", "other")},
        grace_seconds=0.5,
    )


@pytest.fixture
def hosted_process(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Spec 104's hosted mode, as the process environment states it."""
    monkeypatch.setenv("HARRIER_AUTH", "supabase")
    return monkeypatch


@contextmanager
def owners(monkeypatch: pytest.MonkeyPatch, manager: RunManager) -> Generator[Owners]:
    server = KeyServer()
    key = new_ec_key()
    server.publish(KID, key)
    app = create_hosted_app(
        CONFIG, run_manager=manager, fetch_keys=server, clock=Clock(), database_url=None
    )
    # One portal for every request, so the runs the test starts live on the
    # loop the routes read them from.
    with TestClient(app, base_url=f"http://{API_HOST}") as client:
        yield Owners(app, client, manager, key, monkeypatch)


@pytest.fixture
def hosted(hosted_process: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Owners]:
    with owners(hosted_process, hosted_manager(tmp_path)) as built:
        yield built


def new_owner() -> str:
    return str(uuid.uuid4())


def test_the_hosted_app_needs_hosted_mode() -> None:
    """The run manager reads hosted mode from the process environment and
    the app from how it was built, so the hosted app is refused in a local
    process: the two cannot disagree."""
    with pytest.raises(HostedConfigError) as refused:
        create_hosted_app(CONFIG, fetch_keys=KeyServer(), clock=Clock(), database_url=None)
    assert str(refused.value) == HOSTED_APP_REFUSAL


# --- run routes, hosted ---


def test_another_owners_run_is_not_found(hosted: Owners) -> None:
    a, b = new_owner(), new_owner()
    run = hosted.start_local(a)
    client = hosted.client
    try:
        # Owner A sees their own run on every route.
        assert [item["id"] for item in client.get("/runs", headers=hosted.as_owner(a)).json()] == [
            run.id
        ]
        assert client.get(f"/runs/{run.id}", headers=hosted.as_owner(a)).status_code == 200

        # Owner B is told what an id that does not exist would tell them.
        unknown = client.get("/runs/no-such-run", headers=hosted.as_owner(b))
        assert unknown.status_code == 404 and unknown.json() == NOT_FOUND
        assert client.get("/runs", headers=hosted.as_owner(b)).json() == []
        for response in (
            client.get(f"/runs/{run.id}", headers=hosted.as_owner(b)),
            client.post(f"/runs/{run.id}/cancel", headers=hosted.as_owner(b)),
            client.get(f"/runs/{run.id}/events", headers=hosted.as_owner(b)),
        ):
            assert response.status_code == 404, response.text
            assert response.json() == NOT_FOUND
        # B's cancel cancelled nothing.
        time.sleep(0.2)
        assert hosted.run(run.id).state == "running"
        assert not hosted.run(run.id).cancel_requested

        # The view holds the rule itself, not only the routes that ask `get`
        # first: B's stream of A's run is refused as an unknown id is, and
        # A's is not.
        def view_of(owner: str) -> OwnedRuns:
            claims = claims_for(sub=owner)
            scope = {"type": "http", "app": hosted.app, "state": {"claims": claims}, "headers": []}
            return get_runs(Request(scope), hosted.manager)

        with pytest.raises(KeyError):
            view_of(b).stream(run.id)
        with pytest.raises(KeyError):
            view_of(a).stream("no-such-run")
        view_of(a).stream(run.id)

        # A's cancel does.
        cancelled = client.post(f"/runs/{run.id}/cancel", headers=hosted.as_owner(a))
        assert cancelled.status_code == 200, cancelled.text
        assert hosted.run(run.id).cancel_requested
    finally:
        hosted.stop(run.id)


def test_a_run_without_an_owner_is_no_ones(hosted: Owners) -> None:
    run = hosted.start_local("")
    client = hosted.client
    try:
        for owner in (new_owner(), new_owner()):
            headers = hosted.as_owner(owner)
            assert client.get("/runs", headers=headers).json() == []
            for response in (
                client.get(f"/runs/{run.id}", headers=headers),
                client.post(f"/runs/{run.id}/cancel", headers=headers),
                client.get(f"/runs/{run.id}/events", headers=headers),
            ):
                assert response.status_code == 404, response.text
                assert response.json() == NOT_FOUND
        assert hosted.run(run.id).state == "running"
        assert not hosted.run(run.id).cancel_requested

        # A hosted request that reached the manager without a verified
        # `sub` has the empty owner too, and an empty owner sees nothing,
        # not the runs whose owner is empty.
        bare = Request({"type": "http", "app": hosted.app, "state": {}, "headers": []})
        view = get_runs(bare, hosted.manager)
        assert view.hosted and view.owner == ""
        assert view.list_runs() == []
        assert view.get(run.id) is None
        # Nor has it an `exp`, so a stream it opened would end at once.
        assert view.expires_at == 0.0
    finally:
        hosted.stop(run.id)


def test_a_backup_verification_is_its_owners(
    hosted: Owners, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The backups list marks an archive from the verification runs the
    registry holds, which are runs too: hosted, only the caller's."""
    backups = tmp_path / "backups"
    backups.mkdir()
    name = f"{ARCHIVE_PREFIX}2026-01-01-000000{ARCHIVE_SUFFIX}"
    (backups / name).write_bytes(b"not a tar archive")
    monkeypatch.setenv("HARRIER_BACKUP_DIR", str(backups))
    a, b = new_owner(), new_owner()
    params = RunParams(archive=backups / name, switches=frozenset({"--no-follow"}), target=name)
    run = hosted.start_local(a, VERIFY_KIND, params)
    deadline = time.monotonic() + 30
    while hosted.run(run.id).state not in TERMINAL_STATES and time.monotonic() < deadline:
        time.sleep(0.05)
    assert hosted.run(run.id).state == "failed"

    def marked(owner: str) -> object:
        listed = hosted.client.get("/settings/backups", headers=hosted.as_owner(owner))
        assert listed.status_code == 200, listed.text
        return [item["verification"] for item in listed.json()["archives"]]

    assert marked(a) == ["failed"]
    assert marked(b) == [None]


# --- starting a run, hosted ---


@dataclass(frozen=True)
class StartCase:
    method: str
    path: str
    # The run kind the route starts.
    kind: str
    # What the request sends, beyond the bearer token.
    send: dict[str, Any]
    # Whether the route writes an input file before it calls `start`.
    writes_input: bool = False
    # The refusal the route answers itself before it calls `start`, if any:
    # the mail watch is refused hosted for good (spec 109).
    own_refusal: str = ""


# Every route that can start a run, with a request each would accept, so
# the refusal is the only thing in its way. `{job}` is a job in the store
# and `{archive}` an archive in the backups list.
START_CASES: tuple[StartCase, ...] = (
    StartCase("POST", "/runs", "demo", {"json": {"kind": "demo"}}),
    StartCase(
        "POST",
        "/apply/{job}/resume",
        "tailor",
        {"json": {"jd_text": "a pasted job description"}},
        True,
    ),
    StartCase(
        "POST",
        "/apply/{job}/cover-letter",
        "cover-letter",
        {"json": {"notes": "notes for the letter"}},
        True,
    ),
    StartCase(
        "POST", "/apply/{job}/answers", "answers", {"json": {"questions": "why this role?"}}, True
    ),
    StartCase(
        "POST",
        "/apply/{job}/evaluate",
        "evaluate",
        {"json": {"jd_text": "an offer to weigh"}},
        True,
    ),
    StartCase("POST", "/outreach/{job}/find-contacts", "find-contacts", {"json": {}}),
    StartCase(
        "POST", "/outreach/{job}/draft", "outreach-draft", {"json": {"tone": "direct"}}, True
    ),
    StartCase("POST", "/outreach/backfill-posters", "backfill-posters", {"json": {}}),
    StartCase(
        "POST", "/mail/watch", "gmail-watch", {"json": {}}, own_refusal=GMAIL_NOT_HOSTED_DETAIL
    ),
    StartCase("POST", "/ops/feeds", "check-feeds", {}),
    StartCase("POST", "/ops/feeds/prune", "check-feeds", {"json": {"confirm": True}}),
    StartCase("POST", "/ops/reconsider", "reconsider", {"json": {}}),
    StartCase("POST", "/ops/backup", "backup", {"json": {}}),
    StartCase("POST", "/ops/digest", "digest", {"json": {}}),
    StartCase("POST", "/ops/events/backfill", "events-backfill", {"json": {}}),
    StartCase("POST", "/ops/evaluate-prospects", "evaluate-prospects", {"json": {}}),
    StartCase("POST", "/ops/scoring/export", "scoring-export", {}),
    StartCase(
        "POST",
        "/ops/discover",
        "discovery",
        {"files": {"dataset_file": ("jobs.json", b"[]", "application/json")}},
        True,
    ),
    StartCase("POST", "/settings/backups/{archive}/verify", "verify-backup", {}),
)

# Spec 106's table, "Which kinds run hosted", written out here rather than
# read from the code: each refused kind's detail. A kind not listed runs.
SPEC_REFUSALS: dict[str, str] = {
    # Spec 107's third pull request lifted the other six of spec 106's
    # "501 until spec 107" kinds; these two write files no hosted route
    # serves (spec 107, Amendment (2026-10-16): PR 3's build).
    **{
        kind: f"{kind} runs are not available on the hosted API: "
        "no hosted route serves their files (spec 107)"
        for kind in ("outreach-draft", "scoring-export")
    },
    # Spec 109's text, which its own refusal answers (spec 106, Amendment
    # (2026-10-13, implementation), item 9).
    "gmail-watch": GMAIL_NOT_HOSTED_DETAIL,
    "backup": "backup runs are local only",
    "verify-backup": "verify-backup runs are local only",
}

# The routes whose kind spec 106's table refuses hosted, and the others.
REFUSED_CASES = tuple(case for case in START_CASES if case.kind in SPEC_REFUSALS)
OFFERED_CASES = tuple(case for case in START_CASES if case.kind not in SPEC_REFUSALS)

# The routes as the contract names them.
START_ROUTES = {
    (case.method.lower(), case.path.replace("{job}", "{selector}").replace("{archive}", "{name}"))
    for case in START_CASES
}


def northwind_job() -> int:
    """A job the routes can name, added by the test outside any request.
    Hosted, the insert's description digest reads through the seam, which
    refuses with nothing bound (spec 107), so the test binds the local store
    for its own setup."""
    with bind_files(LocalFileStore()):
        conn = connect()
        try:
            return add_job(
                conn,
                {
                    "company": "Northwind Labs",
                    "title": "Senior Engineer",
                    "url": "https://boards.example.com/northwind/1",
                    "source": "greenhouse",
                    "location": "Remote, Europe",
                },
                scope=default_scope(conn),
            )
        finally:
            conn.close()


@pytest.mark.parametrize("case", REFUSED_CASES, ids=lambda case: f"{case.method} {case.path}")
def test_a_kind_not_offered_hosted_answers_501_naming_its_spec(
    case: StartCase,
    hosted_process: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Spec 106, "Which kinds run hosted": every route whose kind the table
    refuses answers 501 with the kind's detail, which names the spec that
    will offer it or says it is local only, and leaves no input file, no
    journal entry, no run and no process."""
    monkeypatch = hosted_process
    # A job and an archive, so each route reaches the run it would start.
    job = northwind_job()
    backups = tmp_path / "backups"
    backups.mkdir()
    archive = f"{ARCHIVE_PREFIX}2026-01-01-000000{ARCHIVE_SUFFIX}"
    (backups / archive).write_bytes(b"an archive's bytes")
    monkeypatch.setenv("HARRIER_BACKUP_DIR", str(backups))

    spawned: list[tuple[object, ...]] = []

    async def spawn(*argv: object, **_: object) -> object:
        spawned.append(argv)
        raise OSError("this test spawns nothing")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    manager = hosted_manager(tmp_path)
    journal = tmp_path / "journal.jsonl"
    # The owner each start was given, before it refused.
    given: list[str] = []
    refusing_start = manager.start

    async def start(
        kind: str, params: Any = None, *, owner: str = "", storage_token: str | None = None
    ) -> Run:
        given.append(owner)
        return await refusing_start(kind, params, owner=owner, storage_token=storage_token)

    monkeypatch.setattr(manager, "start", start)
    caller = new_owner()
    with owners(monkeypatch, manager) as hosted:
        path = case.path.replace("{job}", str(job)).replace("{archive}", archive)
        response = hosted.client.request(
            case.method, path, headers=hosted.as_owner(caller), **case.send
        )
        assert response.status_code == 501, response.text
        assert response.json() == {"detail": SPEC_REFUSALS[case.kind]}
    # A route that writes its input, or refuses on its own, is refused
    # there, before `start`; every other route reached `start` with the
    # caller as the run's owner.
    assert given == ([] if case.writes_input or case.own_refusal else [caller])
    # Nothing exists that the run would have left: no input file, not even
    # the directory, no journal entry, no run and no process.
    assert not run_inputs_dir().exists(), sorted(run_inputs_dir().iterdir())
    assert not journal.exists()
    assert manager.list_runs() == []
    assert spawned == []


@pytest.mark.parametrize("case", OFFERED_CASES, ids=lambda case: f"{case.method} {case.path}")
def test_a_kind_offered_hosted_reaches_its_start(
    case: StartCase,
    hosted_process: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Spec 106: a route whose kind the table offers is not refused hosted.
    It writes its input, if it has one, and reaches `start` with its kind
    and the caller as owner; binding the run is the store's, which these
    apps do not have, so `start` is replaced here (test integrity review of
    PR #316)."""
    monkeypatch = hosted_process
    job = northwind_job()
    manager = hosted_manager(tmp_path)
    given: list[tuple[str, str, str | None]] = []

    async def start(
        kind: str, params: Any = None, *, owner: str = "", storage_token: str | None = None
    ) -> Run:
        given.append((kind, owner, storage_token))
        for path in () if params is None else params.files:
            path.unlink(missing_ok=True)
        return Run(id="offered", kind=kind, command=[], owner=owner)

    monkeypatch.setattr(manager, "start", start)
    caller = new_owner()
    with owners(monkeypatch, manager) as hosted:
        path = case.path.replace("{job}", str(job))
        headers = hosted.as_owner(caller)
        response = hosted.client.request(case.method, path, headers=headers, **case.send)
        assert response.status_code == 200, response.text
    # The run gets the verified request's own token, which it reaches its
    # owner's Storage prefix with (spec 107, Decision taken (2026-10-15)).
    token = headers["Authorization"].removeprefix("Bearer ")
    assert given == [(case.kind, caller, token)]


def test_the_contract_declares_the_hosted_run_refusal() -> None:
    from harrier_api.runs import RunManager as _RunManager

    error_out = {"$ref": "#/components/schemas/ErrorOut"}
    built = create_app(run_manager=_RunManager(kind_commands={})).openapi()
    committed = json.loads(CONTRACT.read_text(encoding="utf-8"))
    for document in (committed, built):
        declared: dict[str, set[tuple[str, str]]] = {}
        for path, operations in document["paths"].items():
            for method, operation in operations.items():
                refusal = operation["responses"].get("501")
                if refusal is None:
                    continue
                assert refusal["content"]["application/json"]["schema"] == error_out
                declared.setdefault(refusal["description"], set()).add((method, path))
        # Every route that starts a run declares spec 106's 501: whether a
        # kind is offered is the table's. The mail routes declare spec 109's
        # own, the read among them, proved by
        # tests/test_tenant_notifications.py::test_hosted_mail_routes_answer_501.
        own = {(case.method.lower(), case.path) for case in START_CASES if case.own_refusal}
        assert declared == {
            RUN_NOT_HOSTED_DESCRIPTION: START_ROUTES - own,
            f"hosted only: {GMAIL_NOT_HOSTED_DETAIL}": own | {("get", "/mail/events")},
        }


# --- locally ---


def test_a_local_run_has_no_owner(tmp_path: Path) -> None:
    manager = hosted_manager(tmp_path)
    with TestClient(create_app(run_manager=manager)) as client:
        started = client.post("/runs", json={"kind": "demo"}, headers=auth())
        assert started.status_code == 200, started.text
        mine = started.json()["id"]
        assert manager.get(mine) is not None and manager.get(mine).owner == ""  # pyright: ignore[reportOptionalMemberAccess]
        # A run with an owner, as a journal from a hosted process could
        # hold: locally every run is listed and every route answers it.
        theirs = on_loop(client, functools.partial(manager.start, "other", owner="someone")).id
        try:
            assert {run["id"] for run in client.get("/runs").json()} == {mine, theirs}
            assert client.get(f"/runs/{theirs}").status_code == 200
            assert client.post(f"/runs/{theirs}/cancel", headers=auth()).status_code == 200
        finally:
            for run_id in (mine, theirs):
                on_loop(client, functools.partial(manager.cancel, run_id))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and any(
                run.state not in TERMINAL_STATES for run in manager.list_runs()
            ):
                time.sleep(0.05)

    lines = [json.loads(line) for line in (tmp_path / "journal.jsonl").read_text().splitlines()]
    assert {line["owner"] for line in lines if line["id"] == mine} == {""}
    assert {line["owner"] for line in lines if line["id"] == theirs} == {"someone"}
    reloaded = RunManager(journal_path=tmp_path / "journal.jsonl", kind_commands={})
    assert {run.id: run.owner for run in reloaded.list_runs()} == {mine: "", theirs: "someone"}


def test_an_old_journal_entry_has_no_owner(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    # The fields a journal entry carried before spec 132.
    entry = {
        "id": "a1b2c3d4e5f6",
        "kind": "demo",
        "state": "succeeded",
        "created_at": "2026-10-01T09:00:00+00:00",
        "started_at": "2026-10-01T09:00:01+00:00",
        "ended_at": "2026-10-01T09:00:05+00:00",
        "exit_code": 0,
        "target": "",
        "inputs": [],
    }
    journal.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    loaded = RunManager(journal_path=journal, kind_commands={}).get("a1b2c3d4e5f6")
    assert loaded is not None
    assert loaded.owner == ""
    assert loaded.state == "succeeded"


# --- the event stream ---


def read_stream(client: TestClient, path: str, headers: dict[str, str]) -> tuple[list[str], float]:
    began = time.monotonic()
    response = client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    return response.text.split("\n"), time.monotonic() - began


def event_ids(lines: list[str]) -> list[int]:
    return [int(line.removeprefix("id: ")) for line in lines if line.startswith("id: ")]


def test_the_event_stream_ends_at_token_expiry(
    hosted_process: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    signal = tmp_path / "go-on"
    manager = hosted_manager(tmp_path, quiet_until(signal))
    owner = new_owner()
    expired_line = TOKEN_EXPIRED_LINE.rstrip("\n")
    with owners(hosted_process, manager) as hosted:
        run = hosted.start_local(owner)
        path = f"/runs/{run.id}/events"
        # The run's first events arrive, then nothing is pending.
        deadline = time.monotonic() + 10
        while len(hosted.run(run.id).events) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(hosted.run(run.id).events) == 2

        # A token that has already passed its `exp`, inside the verifier's
        # leeway: the stream ends at once, before the events it would send.
        now = int(time.time())
        lines, _ = read_stream(hosted.client, path, hosted.as_owner(owner, exp=now - 5))
        assert event_ids(lines) == []
        assert [line for line in lines if line] == [expired_line]

        # A token with at least three seconds left, so a slow machine still
        # opens the stream before `exp`: the pending events, then nothing
        # for a while, then the stream ends at `exp` and not at an event.
        exp = math.ceil(time.time()) + 3
        lines, took = read_stream(hosted.client, path, hosted.as_owner(owner, exp=exp))
        assert event_ids(lines) == [1, 2]
        assert [line for line in lines if line][-1] == expired_line
        assert time.time() >= exp - 0.01
        assert took < 10, took
        assert hosted.run(run.id).state == "running"

        # The browser reconnects with a fresh token and `Last-Event-ID`, and
        # receives the rest, once each, until the run ends.
        signal.write_text("", encoding="utf-8")
        fresh = {**hosted.as_owner(owner), "Last-Event-ID": "2"}
        lines, _ = read_stream(hosted.client, path, fresh)
        assert event_ids(lines) == [3, 4]
        assert expired_line not in lines
        assert hosted.run(run.id).state == "succeeded"
