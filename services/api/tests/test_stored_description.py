"""A job's stored description: read, written, and required (spec 149).

Discovery stores each posting's description by URL. The operator sees it on
the Apply page, pastes one when none was captured, and tailor, cover letter
and answers refuse when there is none, from the CLI and the browser alike.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from conftest import auth
from fastapi.testclient import TestClient

from harrier.apply.description import MAX_DESCRIPTION_BYTES
from harrier.db import connect
from harrier.screening.descriptions import load_cached_description, save_description_cache
from harrier.tracker import get_job
from harrier.tracker.store import add_job
from harrier.tracks import default_scope
from harrier_api.app import create_app
from harrier_api.runs import Run, RunManager, run_inputs_dir
from harrier_cli.main import main

URL = "https://boards.example.com/northwind/1"
POSTING = "Northwind Labs is hiring a remote frontend engineer to own the design system."


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    return tmp_path


def _add(url: str = URL) -> int:
    conn = connect()
    try:
        return add_job(
            conn,
            {
                "company": "Northwind Labs",
                "title": "Senior Frontend Engineer",
                "url": url,
                "source": "greenhouse",
                "location": "Remote, Europe",
            },
            scope=default_scope(conn),
        )
    finally:
        conn.close()


@pytest.fixture
def job_id(env: Path) -> int:
    return _add()


@pytest.fixture
def client(env: Path) -> TestClient:
    return TestClient(create_app())


# --- the routes ---


def test_the_description_route_reads_the_cache(job_id: int, client: TestClient) -> None:
    empty = client.get(f"/apply/{job_id}/description", headers=auth())
    assert empty.status_code == 200
    assert empty.json() == {"text": "", "stored": False}

    save_description_cache(URL, POSTING)
    stored = client.get(f"/apply/{job_id}/description", headers=auth())
    assert stored.status_code == 200
    assert stored.json() == {"text": POSTING, "stored": True}


def test_the_description_route_stores_and_replaces(job_id: int, client: TestClient) -> None:
    first = client.put(
        f"/apply/{job_id}/description", json={"text": f"  {POSTING}\n"}, headers=auth()
    )
    assert first.status_code == 200
    assert first.json() == {"text": POSTING, "stored": True}
    assert load_cached_description(URL) == POSTING

    replaced = client.put(
        f"/apply/{job_id}/description", json={"text": "The posting, corrected."}, headers=auth()
    )
    assert replaced.status_code == 200
    assert load_cached_description(URL) == "The posting, corrected."


@pytest.mark.parametrize(
    ("text", "status", "detail"),
    [
        ("   \n", 400, "the description is empty"),
        ("x" * (MAX_DESCRIPTION_BYTES + 1), 400, f"longer than {MAX_DESCRIPTION_BYTES} bytes"),
        ("a\x00b", 400, "NUL"),
        ("a\ud800b", 400, "not text UTF-8 can encode"),
    ],
)
def test_the_description_route_refuses_what_it_cannot_store(
    job_id: int, client: TestClient, text: str, status: int, detail: str
) -> None:
    save_description_cache(URL, POSTING)
    # Escaped as a browser sends it: a lone surrogate travels as \ud800 in
    # JSON, which the client library will not encode raw.
    response = client.put(
        f"/apply/{job_id}/description",
        content=json.dumps({"text": text}),
        headers={**auth(), "Content-Type": "application/json"},
    )
    assert response.status_code == status
    assert detail in response.json()["detail"]
    # The refused text is never quoted back.
    assert text.strip() == "" or text not in response.text
    # Nothing was stored: the previous entry stands.
    assert load_cached_description(URL) == POSTING


def test_a_job_with_no_url_takes_no_description(env: Path, client: TestClient) -> None:
    no_url = _add(url="")
    read = client.get(f"/apply/{no_url}/description", headers=auth())
    assert read.json() == {"text": "", "stored": False}
    write = client.put(f"/apply/{no_url}/description", json={"text": POSTING}, headers=auth())
    assert write.status_code == 409
    assert write.json()["detail"] == (f"job {no_url} has no URL; a description is stored by URL")


def test_an_unknown_job_is_not_found(env: Path, client: TestClient) -> None:
    assert client.get("/apply/999/description", headers=auth()).status_code == 404
    response = client.put("/apply/999/description", json={"text": POSTING}, headers=auth())
    assert response.status_code == 404


# --- the operations refuse without one ---


@pytest.mark.parametrize(
    ("argv", "domain", "prefix"),
    [
        (["tailor"], "harrier.resume.tailor.run_tailor", "tailor failed"),
        (["cover-letter"], "harrier.apply.generate_cover_letter", "cover letter failed"),
        (
            ["answers", "--question", "Why us?"],
            "harrier.apply.generate_answer_set",
            "answers failed",
        ),
    ],
)
def test_an_operation_without_a_description_refuses(
    job_id: int,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    domain: str,
    prefix: str,
) -> None:
    with patch(domain) as generate:
        code = main([argv[0], "--job-id", str(job_id), *argv[1:]])
    assert code == 1
    assert generate.call_args is None
    assert (
        f"{prefix}: no job description is stored for job {job_id}; "
        f"store one with harrier description set {job_id} --file PATH"
    ) in capsys.readouterr().err
    conn = connect()
    try:
        assert get_job(conn, default_scope(conn), job_id)["status"] == "prospect"
    finally:
        conn.close()


def test_a_stored_description_reaches_the_operation(job_id: int) -> None:
    save_description_cache(URL, POSTING)
    with patch("harrier.resume.tailor.run_tailor") as tailor:
        main(["tailor", "--job-id", str(job_id)])
    assert tailor.call_args is not None
    assert tailor.call_args.kwargs["jd_text"] == POSTING


def _manager_that_records(env: Path) -> tuple[RunManager, list[str]]:
    manager = RunManager(journal_path=env / "data" / "runs" / "journal.jsonl")
    started: list[str] = []

    async def start(kind: str, params: Any = None, **_: Any) -> Run:
        started.append(kind)
        for path in () if params is None else params.files:
            path.unlink(missing_ok=True)
        return Run(id="recorded", kind=kind, command=[])

    manager.start = start  # type: ignore[method-assign]
    return manager, started


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("resume", {}),
        ("resume", {"jd_text": "   "}),
        ("cover-letter", {"notes": "Lead with the design system."}),
        ("answers", {"questions": "Why us?"}),
    ],
)
def test_an_apply_route_without_a_description_starts_no_run(
    job_id: int, env: Path, path: str, body: dict[str, str]
) -> None:
    manager, started = _manager_that_records(env)
    client = TestClient(create_app(run_manager=manager))
    response = client.post(f"/apply/{job_id}/{path}", json=body, headers=auth())
    assert response.status_code == 409
    assert response.json() == {
        "detail": f"no job description is stored for job {job_id}; paste one on the Apply page"
    }
    assert started == []
    assert not run_inputs_dir().exists()


def test_a_given_or_stored_description_starts_the_run(job_id: int, env: Path) -> None:
    manager, started = _manager_that_records(env)
    client = TestClient(create_app(run_manager=manager))
    given = client.post(f"/apply/{job_id}/resume", json={"jd_text": POSTING}, headers=auth())
    assert given.status_code == 200
    save_description_cache(URL, POSTING)
    stored = client.post(f"/apply/{job_id}/cover-letter", json={}, headers=auth())
    assert stored.status_code == 200
    assert started == ["tailor", "cover-letter"]


def test_evaluate_runs_without_a_description(job_id: int, env: Path) -> None:
    manager, started = _manager_that_records(env)
    client = TestClient(create_app(run_manager=manager))
    response = client.post(f"/apply/{job_id}/evaluate", json={}, headers=auth())
    assert response.status_code == 200
    assert started == ["evaluate"]


# --- the CLI verbs ---


def test_the_description_verbs(
    job_id: int, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["description", "show", str(job_id)]) == 1
    assert f"no description stored for job {job_id}" in capsys.readouterr().err

    source = tmp_path / "posting.txt"
    source.write_text(f"{POSTING}\n", encoding="utf-8")
    assert main(["description", "set", str(job_id), "--file", str(source)]) == 0
    assert f"description stored for job {job_id}" in capsys.readouterr().out
    assert load_cached_description(URL) == POSTING

    assert main(["description", "show", str(job_id)]) == 0
    assert capsys.readouterr().out.strip() == POSTING

    source.write_text("  \n", encoding="utf-8")
    assert main(["description", "set", str(job_id), "--file", str(source)]) == 1
    assert "description failed: the description is empty" in capsys.readouterr().err
    assert load_cached_description(URL) == POSTING
