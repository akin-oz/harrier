"""The hosted file store and its binding to a request (spec 107, PR 2).

Storage is a fake behind an `httpx.MockTransport` (`tests/storage_support.py`,
the `storage` fixture in `tests/conftest.py`), so no test reaches the
network. The shapes it answers are the assumptions this spec pins for spec
110's staging check; `test_the_hosted_store_speaks_storages_api` holds the
store to them. Every owner id, token, key and name here is synthetic.
"""

# Pyright strict cannot resolve starlette's TestClient request and response
# members, which is why every API test file carries these.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import uuid
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from storage_support import BUCKET, FakeStorage
from test_hosted_auth import PUBLISHABLE_KEY, SUPABASE_URL, Hosted, bearer, build

from harrier.atomicio import DamagedStateError
from harrier.db import connect, data_dir
from harrier.filestore import (
    CONTENT_TYPES,
    SIZE_LIMIT,
    STORAGE_TOKEN_VARIABLE,
    UNBOUND_TEXT,
    FileStore,
    FileStoreDenied,
    FileStoreError,
    FileStoreUnavailable,
    FileStoreUnbound,
    FileTooLarge,
    LocalFileStore,
    SupabaseFileStore,
    bind_files,
    files,
    load_mapping,
    load_text,
    local_file,
    save_text,
)
from harrier.tracker import add_job, get_job
from harrier.tracker.store import list_events, set_status
from harrier.tracks import default_scope
from harrier_api import app as app_module

OWNER_A = str(uuid.uuid4())
OWNER_B = str(uuid.uuid4())
TOKEN = f"synthetic-token-{uuid.uuid4().hex}"

# A candidate's name and a contact's, as the file names that carry them are
# built (`resume/tailor.py`, `outreach/messages.py`). Synthetic.
CANDIDATE = "deniz-ornek"
CONTACT = "example-person"
PAIR = "example-co-senior-frontend-engineer"


def store_for(owner: str = OWNER_A, token: str = TOKEN) -> SupabaseFileStore:
    return SupabaseFileStore.for_request(
        owner, token, origin=SUPABASE_URL, publishable_key=PUBLISHABLE_KEY
    )


@pytest.fixture
def hosted_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """This process serves hosted, as the hosted API's does."""
    monkeypatch.setenv("HARRIER_AUTH", "supabase")


def sha(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


# --- the REST shapes this spec pins ----------------------------------------------------


def test_the_hosted_store_speaks_storages_api(
    storage: FakeStorage, caplog: pytest.LogCaptureFixture
) -> None:
    """Each operation's method, path, headers and body, as Storage's
    published API gives them (spec 107, Honest limitations, "The REST paths
    are not pinned here": pinned here, for spec 110 to confirm). The HTTP
    client's own request lines, which name the owner and the object, reach
    no log."""
    caplog.set_level(0)
    store = store_for()
    key = "answers/example-co-senior-frontend-engineer.md"
    name = f"{OWNER_A}/answers/{sha(key)}.md"
    base = f"{SUPABASE_URL}/storage/v1/object"

    assert store.read(key) is None
    assert store.stat(key) is None
    store.put(key, "# Answers ü\n".encode(), atomic=True)
    assert store.read(key) == "# Answers ü\n".encode()
    store.append(key, "more\n")
    assert store.read(key) == "# Answers ü\nmore\n".encode()
    entry = store.stat(key)
    assert entry is not None and entry.size == len("# Answers ü\nmore\n".encode())
    assert entry.key == key and entry.mtime > 0 and entry.version
    store.delete(key)
    store.delete(key)
    assert store.read(key) is None

    sent = [(r.method, str(r.url)) for r in storage.requests]
    assert sent == [
        ("GET", f"{base}/{BUCKET}/{name}"),
        ("GET", f"{base}/info/{BUCKET}/{name}"),
        ("POST", f"{base}/{BUCKET}/{name}"),
        ("GET", f"{base}/{BUCKET}/{name}"),
        ("GET", f"{base}/{BUCKET}/{name}"),
        ("POST", f"{base}/{BUCKET}/{name}"),
        ("GET", f"{base}/{BUCKET}/{name}"),
        ("GET", f"{base}/info/{BUCKET}/{name}"),
        ("DELETE", f"{base}/{BUCKET}"),
        ("DELETE", f"{base}/{BUCKET}"),
        ("GET", f"{base}/{BUCKET}/{name}"),
    ]
    for request in storage.requests:
        assert request.headers["apikey"] == PUBLISHABLE_KEY
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
    upload = storage.requests[2]
    assert upload.headers["content-type"] == "text/markdown"
    assert upload.headers["x-upsert"] == "true"
    assert upload.headers["cache-control"] == "no-cache"
    assert upload.content == "# Answers ü\n".encode()
    assert json.loads(storage.requests[8].content) == {"prefixes": [name]}

    # A listed family: names kept, listed page by page, folders skipped.
    exports = "scoring/exports"
    for day in range(1, 4):
        store.put(f"{exports}/features-2026100{day}.jsonl", b"{}\n", atomic=True)
    store.put(f"{exports}/bounded/features-20261001.jsonl", b"{}\n", atomic=True)
    storage.requests.clear()
    listed = store.list(exports)
    assert [item.key for item in listed] == [
        f"{exports}/features-2026100{day}.jsonl" for day in range(1, 4)
    ]
    assert all(item.size == 3 and item.version for item in listed)
    [listing] = storage.requests
    assert (listing.method, str(listing.url)) == ("POST", f"{base}/list/{BUCKET}")
    assert json.loads(listing.content) == {
        "prefix": f"{OWNER_A}/{exports}",
        "limit": 100,
        "offset": 0,
        "sortBy": {"column": "name", "order": "asc"},
    }

    assert [r for r in caplog.records if r.name.startswith(("httpx", "httpcore"))] == []
    assert OWNER_A not in caplog.text

    # Older Storage answers an absent object 400 with statusCode "404".
    storage.legacy_not_found = True
    assert store.read("answers/absent.md") is None
    assert store.stat("answers/absent.md") is None
    store.delete("answers/absent.md")


def test_a_hashed_family_is_not_listed(storage: FakeStorage) -> None:
    """Its names cannot be turned back into keys, so a listing is refused
    before any request rather than answered with names no caller can use."""
    with pytest.raises(FileStoreError, match=r"^list resumes: "):
        store_for().list("resumes")
    assert storage.requests == []


def test_the_seam_exports_its_limit_and_types() -> None:
    """Spec 146 reads both from here and restates neither."""
    assert SIZE_LIMIT == 6_000_000
    assert CONTENT_TYPES == {
        ".pdf": "application/pdf",
        ".md": "text/markdown",
        ".html": "text/html",
        ".json": "application/json",
        ".jsonl": "application/x-ndjson",
    }


# --- which store a call uses ---------------------------------------------------------


def test_an_unbound_hosted_store_refuses(
    monkeypatch: pytest.MonkeyPatch,
    storage: FakeStorage,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Hosted, `files()` with nothing bound raises `FileStoreUnbound`, a
    `RuntimeError` and never an `OSError`, and never returns a local store.
    A route reached without a binding answers 500 with a generic detail,
    logs the route and no key, and writes nothing under the data
    directory."""
    assert isinstance(files(), LocalFileStore)
    monkeypatch.setenv("HARRIER_AUTH", "supabase")
    for call in (
        files,
        lambda: load_text("answers/a.md"),
        lambda: save_text("answers/a.md", "x", atomic=False),
        lambda: local_file("answers/a.md"),
    ):
        with pytest.raises(FileStoreUnbound) as unbound:
            call()
        assert not isinstance(unbound.value, OSError)
    with bind_files(store_for()):
        assert isinstance(files(), SupabaseFileStore)
    with pytest.raises(FileStoreUnbound):
        files()

    # The hosted app built with a binder that binds nothing.
    def no_binder(_config: object) -> None:
        return None

    monkeypatch.setattr(app_module, "storage_binder", no_binder)
    hosted = build(monkeypatch)
    job_id = _job()
    storage.requests.clear()
    caplog.set_level(logging.ERROR)
    response = hosted.client.get(
        f"/apply/{job_id}/artifacts", headers=bearer(hosted.token(sub=OWNER_A))
    )
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal Server Error"}
    assert "no file store bound on GET /apply/{selector}/artifacts" in caplog.text
    assert "cover-letters" not in caplog.text and OWNER_A not in caplog.text
    assert storage.requests == []
    families = {"resumes", "cover-letters", "answers", "reports"}
    assert not [path for path in data_dir().iterdir() if path.name in families]


def _job(company: str = "Example Co", title: str = "Senior Frontend Engineer") -> int:
    """A job in the local store the hosted test app serves. Added outside a
    request, in a hosted process, so a store is bound for the description
    digest the insert reads."""
    with closing(connect()) as conn, bind_files(store_for()):
        return add_job(
            conn,
            {
                "company": company,
                "title": title,
                "url": f"https://example.test/{uuid.uuid4().hex}",
                "source": "manual",
                "status": "shortlisted",
            },
            scope=default_scope(conn),
        )


def _probe_app(hosted: Hosted) -> list[dict[str, Any]]:
    """Two routes on the hosted app: a synchronous one, which FastAPI runs
    in its thread pool, and an async one. Each reports the store
    `files()` returns and writes one answer through it."""
    seen: list[dict[str, Any]] = []
    app: FastAPI = hosted.app

    def sync_probe() -> dict[str, str]:
        store = files()
        assert isinstance(store, SupabaseFileStore)
        save_text("answers/probe.md", "sync", atomic=False)
        seen.append({"route": "sync", "owner": store.owner, "token": store.token})
        return {"ok": "sync"}

    async def async_probe() -> dict[str, str]:
        store = files()
        assert isinstance(store, SupabaseFileStore)
        seen.append({"route": "async", "owner": store.owner, "token": store.token})
        return {"ok": "async"}

    app.add_api_route("/probe/files/sync", sync_probe, methods=["GET"])
    app.add_api_route("/probe/files/async", async_probe, methods=["GET"])
    return seen


def test_a_request_binds_its_owner_for_sync_routes(
    monkeypatch: pytest.MonkeyPatch, storage: FakeStorage
) -> None:
    """Spec 104's middleware binds a `SupabaseFileStore` for the verified
    `sub` and the request's own token, before routing, so a synchronous
    route in the thread pool sees it; two owners' requests each see their
    own; and the binding ends with the response."""
    hosted = build(monkeypatch)
    seen = _probe_app(hosted)
    token_a = hosted.token(sub=OWNER_A)
    token_b = hosted.token(sub=OWNER_B)

    for token in (token_a, token_b):
        assert hosted.client.get("/probe/files/sync", headers=bearer(token)).status_code == 200
        assert hosted.client.get("/probe/files/async", headers=bearer(token)).status_code == 200

    assert [(item["route"], item["owner"], item["token"]) for item in seen] == [
        ("sync", OWNER_A, token_a),
        ("async", OWNER_A, token_a),
        ("sync", OWNER_B, token_b),
        ("async", OWNER_B, token_b),
    ]
    probe = f"answers/{sha('answers/probe.md')}.md"
    assert storage.names() == sorted([f"{OWNER_A}/{probe}", f"{OWNER_B}/{probe}"])
    uploads = [r for r in storage.requests if r.method == "POST"]
    assert [r.headers["authorization"] for r in uploads] == [
        f"Bearer {token_a}",
        f"Bearer {token_b}",
    ]
    assert all(r.headers["apikey"] == PUBLISHABLE_KEY for r in uploads)
    # The binding ended with each response.
    with pytest.raises(FileStoreUnbound):
        files()


# --- object names --------------------------------------------------------------------

HASHED_KEYS = (
    f"resumes/{CANDIDATE}-{PAIR}.pdf",
    f"resumes/{CANDIDATE}-{PAIR}.metadata.json",
    f"cover-letters/{PAIR}.html",
    f"answers/{PAIR}.md",
    f"reports/{PAIR}.md",
    "evaluations/audit.jsonl",
    f"outreach/{PAIR}-candidates.json",
    f"outreach/{PAIR}-recruiter.md",
    f"outreach/messages/{PAIR}-{CONTACT}-recruiter-direct.json",
    "outreach/targets.json",
    f"descriptions/{'a' * 24}.json",
    f"linkedin-page-verdicts/{'b' * 24}.json",
    "discovery/greenhouse_seen.json",
    "discovery/2/apify_academic_seen.json",
    "incoming/greenhouse_latest.json",
    "incoming/2/apify_academic_latest.json",
    "scoring/active-model.json",
    "gmail-watch/events.jsonl",
)
LISTED_KEYS = (
    "discovery/2/apify_academic/runs/run01.json",
    "scoring/exports/features-20261015.jsonl",
    "scoring/exports/bounded/features-20261015.jsonl",
    "scoring/models/job-fit-20261015-0a1b2c3d4e5f.json",
    "scoring/reports/report-20261015.json",
)


def test_object_names_never_carry_a_person(
    monkeypatch: pytest.MonkeyPatch, storage: FakeStorage
) -> None:
    """Every non-listed family's object name is its directory, the SHA-256
    of the whole key and its extension; the listed families keep their
    keys; every name is under the owner's prefix; and no request path
    carries a candidate's or a contact's name. A download still names the
    file by the key's last segment."""
    store = store_for()
    for key in (*HASHED_KEYS, *LISTED_KEYS):
        store.put(key, b"{}", atomic=False)
    expected = {
        f"{OWNER_A}/{key.rsplit('/', 1)[0]}/{sha(key)}{Path(key).suffix}" for key in HASHED_KEYS
    } | {f"{OWNER_A}/{key}" for key in LISTED_KEYS}
    assert set(storage.names()) == expected
    for request in storage.requests:
        for person in (CANDIDATE, CONTACT, "example-co"):
            assert person not in str(request.url)

    # The download, through the artifact routes, as the browser sees it.
    hosted = build(monkeypatch)
    job_id = _job()
    letter = f"cover-letters/{PAIR}.md"
    store.put(letter, "# Letter ü\n".encode(), atomic=False)
    token = bearer(hosted.token(sub=OWNER_A))
    listed = hosted.client.get(f"/apply/{job_id}/artifacts", headers=token)
    assert listed.status_code == 200, listed.text
    rows = {row["kind"]: row for row in listed.json()}
    assert rows["cover-letter-markdown"]["exists"] is True
    assert rows["cover-letter-markdown"]["filename"] == f"{PAIR}.md"
    assert rows["cover-letter-pdf"]["exists"] is False
    served = hosted.client.get(f"/apply/{job_id}/artifacts/cover-letter-markdown", headers=token)
    assert served.status_code == 200
    assert served.content == "# Letter ü\n".encode()
    assert served.headers["content-type"] == "text/markdown; charset=utf-8"
    assert served.headers["content-disposition"] == f'attachment; filename="{PAIR}.md"'
    absent = hosted.client.get(f"/apply/{job_id}/artifacts/cover-letter-pdf", headers=token)
    assert absent.status_code == 404
    assert absent.json() == {
        "detail": "no cover-letter-pdf for this job yet; run cover-letter to produce it"
    }
    # Another owner's request finds nothing under the same key.
    other = bearer(hosted.token(sub=OWNER_B))
    rows_b = {
        row["kind"]: row
        for row in hosted.client.get(f"/apply/{job_id}/artifacts", headers=other).json()
    }
    assert rows_b["cover-letter-markdown"]["exists"] is False


def test_a_download_names_the_file_as_a_local_one_does(tmp_path: Path) -> None:
    """The hosted `Content-Disposition` is the one `FileResponse` builds
    for the same name, plain and with characters it must quote."""
    from fastapi.responses import FileResponse

    path = tmp_path / "file.md"
    path.write_text("x")
    for name in (f"{PAIR}.md", "örnek cv.md"):
        local = FileResponse(path, filename=name).headers["content-disposition"]
        assert app_module.attachment_disposition(name) == local


# --- errors ----------------------------------------------------------------------------


def test_storage_errors_name_the_family_only(
    storage: FakeStorage, hosted_process: None, caplog: pytest.LogCaptureFixture
) -> None:
    """Each Storage answer in spec 107's error table maps to its class, and
    no message names the owner, the token, the full key or the object name:
    the operation and the key's first segment only. The same holds for a
    damaged state file's message and the model's log line, which named the
    qualified name before."""
    store = store_for()
    key = f"resumes/{CANDIDATE}-{PAIR}.pdf"
    name = store.object_name(key)
    forbidden = (OWNER_A, TOKEN, key, name, sha(key), CANDIDATE)

    def clean(text: str) -> None:
        for value in forbidden:
            assert value not in text, value

    cases: list[tuple[int | Exception, type[FileStoreError]]] = [
        (401, FileStoreDenied),
        (403, FileStoreDenied),
        (413, FileTooLarge),
        (500, FileStoreUnavailable),
        (503, FileStoreUnavailable),
        (418, FileStoreError),
        (httpx.ConnectError(f"refused for {name}"), FileStoreUnavailable),
        (httpx.ReadTimeout(f"timed out for {name}"), FileStoreUnavailable),
    ]
    operations = {
        "download": ("read", lambda: store.read(key)),
        "upload": ("put", lambda: store.put(key, b"%PDF", atomic=True)),
        "info": ("stat", lambda: store.stat(key)),
        "remove": ("delete", lambda: store.delete(key)),
    }
    for answer, raised in cases:
        for fake_operation, (verb, call) in operations.items():
            if isinstance(answer, Exception):
                storage.raising[fake_operation] = answer
            else:
                storage.failures[fake_operation] = [answer]
            with pytest.raises(raised) as caught:
                call()
            storage.raising.clear()
            assert type(caught.value) is raised, (answer, verb)
            message = str(caught.value)
            assert message.startswith(f"{verb} resumes: "), message
            clean(message)

    # Refused before any request: too large, or a type the bucket refuses.
    storage.requests.clear()
    with pytest.raises(FileTooLarge, match=r"^put resumes: "):
        store.put(key, b"x" * (SIZE_LIMIT + 1), atomic=True)
    with pytest.raises(FileTooLarge, match=r"^append gmail-watch: "):
        store.append("gmail-watch/events.jsonl", "x" * (SIZE_LIMIT + 1))
    with pytest.raises(FileStoreError, match=r"^put resumes: the bucket accepts no file"):
        store.put(f"resumes/{CANDIDATE}.docx", b"x", atomic=True)
    assert storage.requests == []

    # A damaged state file and the model's log line, hosted.
    with bind_files(store):
        storage.put(store.object_name("discovery/greenhouse_seen.json"), b"{not json")
        with pytest.raises(DamagedStateError) as damaged:
            load_mapping("discovery/greenhouse_seen.json")
        clean(str(damaged.value))
        assert str(damaged.value).startswith("discovery ")

        from harrier.scoring import model as model_module

        model_module._cache.clear()  # pyright: ignore[reportPrivateUsage]
        model_module._warned.clear()  # pyright: ignore[reportPrivateUsage]
        caplog.set_level(logging.INFO)
        storage.failures["info"] = [503]
        assert model_module.load_active_model() == (None, model_module.MODEL_INVALID)
        assert model_module.load_active_model() == (None, model_module.MODEL_MISSING)
        lines = [r.getMessage() for r in caplog.records if r.name == model_module.__name__]
        assert len(lines) == 2 and all(" at scoring)" in line for line in lines), lines
        for line in lines:
            clean(line)


def test_an_artifact_route_answers_503_when_storage_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, storage: FakeStorage, caplog: pytest.LogCaptureFixture
) -> None:
    """Unavailable Storage is the declared 503 with its fixed body; Storage
    refusing the owner's own token is a 500, logged with the operation and
    the family and never a key (spec 107, Failure modes). The contract
    declares the 503 body on every route that uses the seam."""
    hosted = build(monkeypatch)
    job_id = _job()
    store_for().put(f"cover-letters/{PAIR}.md", b"# Letter\n", atomic=False)
    token = bearer(hosted.token(sub=OWNER_A))
    caplog.set_level(logging.WARNING)

    storage.failures["info"] = [503]
    listed = hosted.client.get(f"/apply/{job_id}/artifacts", headers=token)
    assert listed.status_code == 503
    assert listed.json() == {
        "detail": "Storage cannot be reached now. Try again shortly.",
        "reason": "storage_unavailable",
    }
    storage.failures["download"] = [502]
    served = hosted.client.get(f"/apply/{job_id}/artifacts/cover-letter-markdown", headers=token)
    assert served.status_code == 503
    storage.failures["info"] = [403]
    denied = hosted.client.get(f"/apply/{job_id}/artifacts", headers=token)
    assert denied.status_code == 500
    assert denied.json() == {"detail": "Internal Server Error"}
    assert "storage refused on GET /apply/{selector}/artifacts: stat cover-letters: " in caplog.text
    for value in (OWNER_A, PAIR, CANDIDATE):
        assert value not in caplog.text

    declared = hosted.app.openapi()["paths"]
    for path, method in (
        ("/apply/{selector}/artifacts", "get"),
        ("/apply/{selector}/artifacts/{kind}", "get"),
        ("/outreach/{selector}/candidates/approve", "post"),
        ("/outreach/{selector}/candidates/reject", "post"),
    ):
        schema = declared[path][method]["responses"]["503"]["content"]["application/json"]
        refs = {item["$ref"].rsplit("/", 1)[-1] for item in schema["schema"]["anyOf"]}
        assert "StorageUnavailableOut" in refs, path


def test_approve_and_reject_answer_503_when_storage_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, storage: FakeStorage
) -> None:
    """An outage while reading the staged candidates is the declared 503,
    and a denied read a 500, on approve and reject alike: never a 404 that
    calls a staged candidate unknown (data-integrity review of PR #363)."""
    from harrier.outreach.discovery import candidates_key

    hosted = build(monkeypatch)
    job_id = _job()
    staged = {"candidates": [{"linkedin_url": "https://www.linkedin.com/in/example-person"}]}
    store_for().put(
        candidates_key("Example Co", "Senior Frontend Engineer"),
        json.dumps(staged).encode("utf-8"),
        atomic=False,
    )
    token = bearer(hosted.token(sub=OWNER_A))
    body = {"linkedin_url": "https://www.linkedin.com/in/example-person"}
    for route in ("approve", "reject"):
        storage.failures["download"] = [503]
        answer = hosted.client.post(
            f"/outreach/{job_id}/candidates/{route}", headers=token, json=body
        )
        assert answer.status_code == 503, (route, answer.json())
        assert answer.json()["reason"] == "storage_unavailable"
        storage.failures["download"] = [403]
        denied = hosted.client.post(
            f"/outreach/{job_id}/candidates/{route}", headers=token, json=body
        )
        assert denied.status_code == 500, (route, denied.json())


# --- reads keep their rules --------------------------------------------------------------


def test_an_outage_never_reads_as_empty_state(storage: FakeStorage, hosted_process: None) -> None:
    """Hosted, unavailable Storage raises `DamagedStateError` for seen
    state and mail state, never an empty state (spec 040), while a status
    change still records, with an empty description digest (spec 079)."""
    from harrier.mail.watch import load_state
    from harrier.screening.descriptions import save_description_cache
    from harrier.screening.seen import load_seen

    with bind_files(store_for()):
        storage.failures["download"] = [503]
        with pytest.raises(DamagedStateError):
            load_seen("greenhouse")
        storage.failures["download"] = [503]
        with pytest.raises(DamagedStateError):
            load_seen("apify_academic", 2)
        storage.failures["download"] = [503]
        with pytest.raises(DamagedStateError):
            load_state()
        # Absent is still the first run.
        assert load_seen("greenhouse") == {}

        conn = connect()
        try:
            scope = default_scope(conn)
            job_id = add_job(
                conn,
                {
                    "company": "Example Co",
                    "title": "Senior Frontend Engineer",
                    "url": "https://example.test/jobs/7",
                    "source": "manual",
                },
                scope=scope,
            )
            save_description_cache("https://example.test/jobs/7", "Remote across Europe.")
            storage.failures["download"] = [503]
            set_status(conn, scope, job_id, "shortlisted")
            assert get_job(conn, scope, job_id)["status"] == "shortlisted"
            assert list_events(conn, scope, job_id)[-1]["description_sha256"] == ""
        finally:
            conn.close()
    assert not (data_dir() / "descriptions").exists()


# --- the model cache -----------------------------------------------------------------------


def test_the_model_cache_never_crosses_owners(storage: FakeStorage, hosted_process: None) -> None:
    """Two owners, the same key, two models. Storage gives both objects the
    same version, so only the owner in the cache's key tells them apart:
    each owner is scored by their own model, in any order."""
    from harrier.scoring import model as model_module
    from harrier.scoring.features import FEATURE_ORDER
    from harrier.scoring.model import ACTIVE_MODEL_KEY, dump_model, model_document, model_identity

    def document(intercept: float) -> bytes:
        return dump_model(
            model_document(
                coefficients=[0.1 for _ in FEATURE_ORDER],
                intercept=intercept,
                p92={"skill_signal": 30.0, "preferred_signal": 12.0, "years_gap": 5.0},
                created_at="2026-10-15",
                training={"rows": 0, "positives": 0, "split_at": "2026-10-15"},
                evaluation={"metric": "average_precision"},
            )
        )

    first, second = document(-1.0), document(-2.0)
    store_a, store_b = store_for(OWNER_A), store_for(OWNER_B)
    store_a.put(ACTIVE_MODEL_KEY, first, atomic=True)
    store_b.put(ACTIVE_MODEL_KEY, second, atomic=True)
    for name in (store_a.object_name(ACTIVE_MODEL_KEY), store_b.object_name(ACTIVE_MODEL_KEY)):
        storage.objects[name].etag = '"same"'
        storage.objects[name].updated_at = "2026-10-16T09:00:00Z"

    model_module._cache.clear()  # pyright: ignore[reportPrivateUsage]
    identities: list[str] = []
    for store in (store_a, store_b, store_a, store_b):
        with bind_files(store):
            loaded, reason = model_module.load_active_model()
        assert reason is None and loaded is not None
        identities.append(loaded.identity)
    assert identities == [model_identity(first), model_identity(second)] * 2


# --- hosted PDF rendering ------------------------------------------------------------------


def test_a_hosted_pdf_uploads_only_after_the_gate(
    storage: FakeStorage, hosted_process: None
) -> None:
    """The earlier object goes first. Chromium renders into a new directory
    of mode 0700, the gate runs there, and only a passing PDF is uploaded,
    once. The directory is gone afterwards, pass or fail, and nothing lands
    under the data directory."""
    from harrier.resume.pdf import render_validated_pdf

    key = f"resumes/{CANDIDATE}-{PAIR}.pdf"
    store = store_for()
    rendered: list[Path] = []
    modes: list[int] = []

    def render(html_text: str, pdf_path: Path) -> None:
        rendered.append(pdf_path)
        modes.append(stat.S_IMODE(pdf_path.parent.stat().st_mode))
        pdf_path.write_bytes(b"%PDF-1.4 synthetic")

    def failing(pdf_path: Path, html_text: str) -> list[str]:
        assert pdf_path.read_bytes() == b"%PDF-1.4 synthetic"
        return ["rendered PDF has 2 pages; expected 1"]

    def passing(pdf_path: Path, html_text: str) -> list[str]:
        return []

    with bind_files(store):
        store.put(key, b"%PDF-1.4 earlier", atomic=True)
        storage.requests.clear()
        errors = render_validated_pdf("<html></html>", key, render, failing)
        assert errors == ["rendered PDF has 2 pages; expected 1"]
        assert store.read(key) is None
        assert [r.method for r in storage.requests if r.method == "POST"] == []

        storage.requests.clear()
        assert render_validated_pdf("<html></html>", key, render, passing) == []
        uploads = [r for r in storage.requests if r.method == "POST"]
        assert len(uploads) == 1
        assert uploads[0].headers["content-type"] == "application/pdf"
        assert store.read(key) == b"%PDF-1.4 synthetic"

        def broken(html_text: str, pdf_path: Path) -> None:
            rendered.append(pdf_path)
            raise RuntimeError("the renderer stopped")

        with pytest.raises(RuntimeError):
            render_validated_pdf("<html></html>", key, broken, passing)
        assert store.read(key) is None

    assert modes == [0o700, 0o700]
    assert len({path.parent for path in rendered}) == 3
    assert not [path for path in rendered if path.parent.exists()]
    assert not (data_dir() / "resumes").exists()


# --- the owner ---------------------------------------------------------------------------


def test_a_hosted_store_takes_only_an_owner_id(storage: FakeStorage) -> None:
    """The prefix is the verified `sub` in its canonical lowercase form, and
    nothing that is not a UUID becomes one."""
    for owner in ("not-an-owner", "", "../", f"{OWNER_A}/x"):
        with pytest.raises(ValueError):
            store_for(owner=owner)
    store = store_for(owner=OWNER_A.upper())
    assert store.owner == OWNER_A
    assert store.object_name("scoring/reports/report-20261015.json").startswith(f"{OWNER_A}/")
    assert storage.requests == []


# --- a missing binding is not a failed source ------------------------------------------


def test_academic_discovery_never_reads_an_unbound_store_as_a_failed_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run_academic_discovery` records a failing source and returns. A
    store that was never bound is not one: `FileStoreUnbound` is a
    `RuntimeError`, which that handler catches, so it is let through first
    (spec 107, Amendment (2026-10-15): PR 1's build)."""
    from academic_support import academic_search_entry, fixture_items

    import harrier.academic.discovery as academic_module
    from harrier.academic.discovery import AcademicOptions, run_academic_discovery
    from harrier.sources import apify_academic
    from harrier.tracks import add_track, resolve_scope
    from harrier.userconfig.store import ACADEMIC_SEARCHES, set_config

    monkeypatch.setenv(apify_academic.TOKEN_ENV, "token-sentinel-value")
    monkeypatch.setenv(apify_academic.ACTOR_ENV, "actor-sentinel-name")
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    monkeypatch.delenv(apify_academic.PORTALS_ENV, raising=False)
    fetched: list[str] = []

    def run_actor(*_args: object, **_kwargs: object) -> apify_academic.ActorRun:
        fetched.append("run")
        return apify_academic.ActorRun(
            run_id="run01", status="SUCCEEDED", dataset_id="set01", items=fixture_items()
        )

    monkeypatch.setattr(academic_module.source, "run_actor", run_actor)
    conn = connect()
    try:
        add_track(conn, "second-search", "academic", "Second search")
        set_config(conn, ACADEMIC_SEARCHES, {"second-search": academic_search_entry()})
        scope = resolve_scope(conn, "second-search")
        monkeypatch.setenv("HARRIER_AUTH", "supabase")
        with pytest.raises(FileStoreUnbound):
            run_academic_discovery(
                conn, scope, AcademicOptions(now=datetime(2026, 6, 15, 10, 0), notify=False)
            )
    finally:
        conn.close()
    assert fetched == ["run"]


# --- a hosted run's binding (spec 107, PR 3) -------------------------------------------


def test_a_hosted_run_binds_its_owners_storage(
    storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Spec 107, Amendment (2026-10-13): in a hosted run, `files()` is bound
    to the run's owner before any domain code runs, writes only under that
    owner's prefix with the token the run was given, and never returns the
    local store. The token leaves the run's environment as it is bound.
    Without a token the run binds nothing and its first file refuses, in one
    line, with nothing on the machine's disk."""
    from harrier_cli import main as cli

    data = data_dir()
    monkeypatch.delenv("HARRIER_AUTH", raising=False)
    monkeypatch.setenv("HARRIER_DATABASE_URL", "postgresql://run:synthetic@127.0.0.1:1/postgres")
    monkeypatch.setenv("HARRIER_RUN_OWNER", OWNER_A)
    monkeypatch.setenv("HARRIER_SUPABASE_URL", SUPABASE_URL)
    monkeypatch.setenv("HARRIER_SUPABASE_PUBLISHABLE_KEY", PUBLISHABLE_KEY)
    monkeypatch.setenv(STORAGE_TOKEN_VARIABLE, TOKEN)
    # Its setup opens the run's store, which this test has none of.
    monkeypatch.setattr(cli, "configure_logging", lambda: None)
    seen: list[tuple[FileStore, str | None]] = []

    def domain(_args: object) -> int:
        seen.append((files(), os.environ.get(STORAGE_TOKEN_VARIABLE)))
        save_text("answers/example-co-senior-frontend-engineer.md", "# Drafted\n", atomic=False)
        return 0

    monkeypatch.setattr(cli, "_cmd_demo_run", domain)
    argv = ["demo-run", "--steps", "1", "--delay", "0"]

    assert cli.main(argv) == 0

    [(store, left)] = seen
    assert isinstance(store, SupabaseFileStore)
    assert (store.owner, store.token) == (OWNER_A, TOKEN)
    assert left is None
    key = "answers/example-co-senior-frontend-engineer.md"
    assert storage.names() == [f"{OWNER_A}/answers/{sha(key)}.md"]
    assert {r.headers["authorization"] for r in storage.requests} == {f"Bearer {TOKEN}"}
    assert not data.exists() or not any(path.is_file() for path in data.rglob("*"))

    # No token: nothing bound, and the first file refuses.
    seen.clear()
    storage.requests.clear()
    assert cli.main(argv) == 1
    assert capsys.readouterr().err.strip().splitlines()[-1] == f"error: {UNBOUND_TEXT}"
    assert seen == [] and storage.requests == []
    assert not data.exists() or not any(path.is_file() for path in data.rglob("*"))


def _hosted_academic_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from test_filestore import _academic_scope  # pyright: ignore[reportPrivateUsage]

    from harrier.academic.discovery import save_dataset

    with closing(connect()) as conn:
        scope = _academic_scope(monkeypatch, conn)
    for run_id in ("run01", "run02"):
        save_dataset(scope, run_id, [{"id": "porta_0001", "title": "Researcher"}])


def _hosted_scoring_exports(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from harrier.scoring.export import export_features
    from harrier.tracks import add_track, resolve_scope

    with closing(connect()) as conn:
        export_features(conn, default_scope(conn), today="2026-10-15")
        add_track(conn, "bounded", "focused", "Bounded roles")
        export_features(conn, resolve_scope(conn, "bounded"), today="2026-10-15")


def _hosted_scoring_models(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from test_filestore import _training_export  # pyright: ignore[reportPrivateUsage]

    import harrier.scoring.train as train_module

    # The training input is made beside, never in, the data directory, and
    # placed in the owner's prefix as an export run would have left it.
    elsewhere = tmp_path / "training-input"
    with monkeypatch.context() as scoped:
        scoped.setenv("HARRIER_DATA_DIR", str(elsewhere))
        with bind_files(LocalFileStore(elsewhere)):
            _training_export()
    name = "scoring/exports/features-20261006.jsonl"
    files().put(name, (elsewhere / name).read_bytes(), atomic=True)
    metrics = {"model": 0.9, "baseline": 0.5, "delta_ci95": [0.1, 0.2]}

    def evaluate(*_args: object, **_kwargs: object) -> dict[str, object]:
        return dict(metrics)

    monkeypatch.setattr(train_module, "evaluate", evaluate)
    assert train_module.train(activate=True, today="2026-10-15").activated


def _hosted_cases() -> dict[str, Callable[[pytest.MonkeyPatch, Path], object]]:
    """Every family of spec 107's first table, through the writer PR 1's
    characterization test runs (`tests/test_filestore.py`), except the three
    whose cases read the local files they wrote; those run their writers
    here."""
    from test_filestore import CASES

    cases: dict[str, Callable[[pytest.MonkeyPatch, Path], object]] = dict(CASES)
    cases["academic-runs"] = _hosted_academic_runs
    cases["scoring-exports"] = _hosted_scoring_exports
    cases["scoring-models"] = _hosted_scoring_models
    return cases


# The families each case must reach in the owner's prefix.
HOSTED_FAMILIES: dict[str, set[str]] = {
    "academic-incoming": {"incoming"},
    "academic-runs": {"discovery"},
    "answers": {"answers"},
    "cover-letters": {"cover-letters"},
    "descriptions": {"descriptions"},
    "discovery-seen": {"discovery"},
    "evaluations": {"evaluations"},
    "gmail-watch": {"gmail-watch"},
    "incoming": {"incoming"},
    "linkedin-page-verdicts": {"linkedin-page-verdicts"},
    "outreach": {"outreach"},
    "reports": {"reports"},
    "resumes": {"resumes"},
    "resumes-focused": {"resumes"},
    "scoring-exports": {"scoring"},
    "scoring-models": {"scoring"},
}

# The local tracker database: the store, not a file the seam writes.
STORE_FILES = re.compile(r"tracker\.db(-wal|-shm|-journal)?")


@pytest.mark.parametrize("family", sorted(HOSTED_FAMILIES))
def test_a_hosted_store_writes_nothing_under_the_data_directory(
    family: str, storage: FakeStorage, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Spec 107, PR 2's criterion, built by PR 3: every writer in the first
    table, run hosted against the fake Storage server, writes nothing under
    `data_dir()` and uploads only under `<owner>/`. The process is a hosted
    run (`HARRIER_RUN_OWNER`), so a writer that forgot the binding would
    refuse rather than fall back to the disk. The local tracker database is
    the only file left there: it is the store these writers record into, not
    one of their files."""
    assert set(_hosted_cases()) == set(HOSTED_FAMILIES)
    monkeypatch.setenv("HARRIER_RUN_OWNER", OWNER_A)
    with bind_files(store_for()):
        _hosted_cases()[family](monkeypatch, tmp_path)

    root = data_dir()
    left = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not STORE_FILES.fullmatch(path.name)
    )
    assert left == []
    names = storage.names()
    assert names and all(name.startswith(f"{OWNER_A}/") for name in names), names
    uploaded = {name.split("/", 2)[1] for name in names}
    assert HOSTED_FAMILIES[family] <= uploaded, uploaded
    # And none of them reached Storage under another prefix or none.
    sent = [storage.operation(request) for request in storage.requests]
    assert all(
        name.startswith(f"{OWNER_A}/") for operation, name in sent if operation != "remove" and name
    )


def test_a_hosted_render_tool_inherits_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """In a hosted run, Chromium and `pdfinfo` start with only the names a
    render needs: not the run login's URL, not the owner's AI key. Locally
    both still inherit the process's environment (security review of
    PR #368)."""
    import subprocess
    import sys
    import types
    from contextlib import contextmanager

    from harrier.resume import pdf as pdf_module

    seen: dict[str, dict[str, str] | None] = {}

    @contextmanager
    def fake_playwright() -> Any:
        seen["chromium"] = dict(os.environ)

        class Page:
            def set_content(self, *_args: object, **_kwargs: object) -> None: ...
            def emulate_media(self, *_args: object, **_kwargs: object) -> None: ...
            def pdf(self, *, path: str, **_kwargs: object) -> None:
                Path(path).write_bytes(b"%PDF-1.4")

        class Browser:
            def new_page(self) -> Page:
                return Page()

            def close(self) -> None: ...

        class Chromium:
            def launch(self) -> Browser:
                return Browser()

        yield types.SimpleNamespace(chromium=Chromium())

    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = fake_playwright  # type: ignore[attr-defined]
    module.Error = RuntimeError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)

    def fake_run(*_args: object, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["pdfinfo"] = kwargs.get("env")
        return subprocess.CompletedProcess([], 0, stdout="Pages: 1\n", stderr="")

    monkeypatch.setattr(pdf_module.subprocess, "run", fake_run)
    secret_url = "postgresql://harrier_run.example:synthetic-password@db.example.test:5432/x"
    monkeypatch.setenv("HARRIER_DATABASE_URL", secret_url)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-ai-key")
    monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin"))

    for hosted in (True, False):
        if hosted:
            monkeypatch.setenv("HARRIER_RUN_OWNER", str(uuid.uuid4()))
        else:
            monkeypatch.delenv("HARRIER_RUN_OWNER", raising=False)
            monkeypatch.delenv("HARRIER_AUTH", raising=False)
        target = data_dir() / f"render-{hosted}.pdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        pdf_module.render_pdf("<p>x</p>", target)
        pdf_module.validate_rendered_pdf(target, "<p>x</p>", allowed_pages=(1,))
        chromium = seen["chromium"]
        assert chromium is not None
        if hosted:
            assert chromium["HARRIER_DATABASE_URL"] == ""
            assert "ANTHROPIC_API_KEY" not in chromium
            assert "PATH" in chromium
            pdfinfo = seen["pdfinfo"]
            assert pdfinfo is not None
            assert pdfinfo["HARRIER_DATABASE_URL"] == ""
            assert "ANTHROPIC_API_KEY" not in pdfinfo
            assert set(pdfinfo) <= {*pdf_module.RENDER_ENVIRONMENT, "HARRIER_DATABASE_URL"}
            # The run's own environment is back after the render.
            assert os.environ["HARRIER_DATABASE_URL"] == secret_url
        else:
            assert chromium["HARRIER_DATABASE_URL"] == secret_url
            assert seen["pdfinfo"] is None


# --- the data-integrity review of PR #368 --------------------------------------


def test_backfill_never_rewrites_the_candidates_on_a_read_error(storage: FakeStorage) -> None:
    """A Storage error while `backfill-posters` reads the staged candidates
    ends that poster: it never reads as "nothing staged" and uploads a fresh
    list over the candidates and their reviews."""
    from harrier.outreach.discovery import candidates_key
    from harrier.outreach.posters import stage_poster

    store = store_for()
    key = candidates_key("Example Co", "Senior Frontend Engineer")
    staged = {
        "candidates": [
            {"person_name": "Example Approved", "review_status": "approved"},
            {"person_name": "Example Pending", "review_status": "pending"},
        ]
    }
    store.put(key, json.dumps(staged).encode("utf-8"), atomic=False)
    poster = {
        "name": "Example Poster",
        "linkedin_url": "https://www.linkedin.com/in/example-poster",
    }

    with bind_files(store):
        storage.failures["download"] = [503]
        with pytest.raises(FileStoreError):
            stage_poster(
                "Example Co",
                "Senior Frontend Engineer",
                "https://jobs.example.com/example-co",
                poster,
                source="linkedin_guest",
                raw_query="guest_poster_backfill",
            )

    stored = store.read(key)
    assert stored is not None
    assert json.loads(stored) == staged


def test_an_unreadable_description_is_logged_and_reads_as_empty(
    storage: FakeStorage, caplog: pytest.LogCaptureFixture
) -> None:
    """The description cache reads an unavailable Storage as no description,
    as spec 079 requires, and logs it by class and family only: never the
    posting's URL or the key."""
    from harrier.screening.descriptions import load_cached_description

    url = "https://jobs.example.com/example-co/senior-frontend-engineer"
    caplog.set_level(logging.WARNING)
    with bind_files(store_for()):
        storage.failures["download"] = [503]
        assert load_cached_description(url) == ""
    assert "description cache read as empty: FileStoreUnavailable on descriptions" in caplog.text
    assert url not in caplog.text
    assert OWNER_A not in caplog.text


def test_a_real_command_without_a_store_refuses_in_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A real verb, not a stub: `tailor` in a hosted run with no token stops
    at its first file with exit 1, one line naming the refusal, and nothing
    on the machine's disk. The command words the line itself
    (`tailor failed: ...`), as spec 107's PR 3 amendment allows."""
    from resume_support import example_documents, store_documents

    from harrier_cli import main as cli

    with closing(connect()) as conn:
        store_documents(conn, *example_documents())
    job_id = _job()
    before = sorted(str(path) for path in data_dir().rglob("*") if path.is_file())
    monkeypatch.setenv("HARRIER_RUN_OWNER", OWNER_A)
    monkeypatch.delenv(STORAGE_TOKEN_VARIABLE, raising=False)
    monkeypatch.setattr(cli, "_open_store", connect)
    args = cli.build_parser().parse_args(["tailor", "--job-id", str(job_id), "--no-ai"])

    assert cli._cmd_tailor(args) == 1  # pyright: ignore[reportPrivateUsage]

    last = capsys.readouterr().err.strip().splitlines()[-1]
    assert last == f"tailor failed: {UNBOUND_TEXT}"
    after = sorted(str(path) for path in data_dir().rglob("*") if path.is_file())
    assert after == before
