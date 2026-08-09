"""Batch export and capture pins (spec 010), ported from the old repo's
tests/test_feed_importers.py and tests/test_job_server.py."""

import csv
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harrier.capture import add_captured_job
from harrier.db import connect
from harrier.sources.batch_exports import (
    normalize_wellfound_row,
    normalize_wttj_row,
    read_export_rows,
)
from harrier.tracker import list_jobs
from harrier_api.app import create_app

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false


def test_wellfound_export_normalization() -> None:
    job = normalize_wellfound_row(
        {
            "company_name": "Wellfound Co",
            "job_title": "Senior Frontend Engineer",
            "location": "Remote, Europe",
            "job_url": "https://wellfound.example/jobs/1",
            "job_description": "Remote Europe role with React and TypeScript.",
            "job_id": "wf-1",
        }
    )
    assert job["source"] == "wellfound"
    assert job["external_id"] == "wf-1"


def test_wttj_export_normalization() -> None:
    job = normalize_wttj_row(
        {
            "company_name": "WTTJ Co",
            "job_title": "Senior Frontend Engineer",
            "location": "Remote, Europe",
            "job_url": "https://wttj.example/jobs/1",
            "description": "Remote Europe role with React and TypeScript.",
            "slug": "wttj-1",
        }
    )
    assert job["source"] == "wttj"
    assert job["external_id"] == "wttj-1"


def test_read_export_rows_csv_json_and_container(tmp_path: Path) -> None:
    csv_path = tmp_path / "export.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["company", "title"])
        writer.writeheader()
        writer.writerow({"company": "A", "title": "T"})
    assert read_export_rows(csv_path) == [{"company": "A", "title": "T"}]

    json_path = tmp_path / "export.json"
    json_path.write_text(json.dumps([{"company": "B"}]), encoding="utf-8")
    assert read_export_rows(json_path) == [{"company": "B"}]

    container_path = tmp_path / "wrapped.json"
    container_path.write_text(json.dumps({"items": [{"company": "C"}]}), encoding="utf-8")
    assert read_export_rows(container_path) == [{"company": "C"}]


@pytest.fixture()
def capture_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    monkeypatch.chdir(Path(__file__).resolve().parents[3])  # repo root for config/
    return tmp_path


def test_add_captured_job_scores_and_marks_manual(capture_env: Path) -> None:
    conn = connect()
    result = add_captured_job(
        conn,
        company="Acme",
        title="Senior Frontend Engineer",
        location="Remote, Europe",
        url="https://example.com/jobs/1",
        description="Remote Europe role with TypeScript and React. " * 200,
    )
    assert result.status == "added"
    job = list_jobs(conn)[0]
    assert job["status"] == "prospect"
    assert "manual_added=" in job["notes"]
    assert "score=" in job["notes"]
    assert job["source_label"].startswith("manual:")

    duplicate = add_captured_job(conn, company="Acme", title="Senior Frontend Engineer")
    assert duplicate.status == "duplicate"

    invalid = add_captured_job(conn, company="", title="X")
    assert invalid.status == "invalid"


def test_capture_endpoints_status_contract(capture_env: Path) -> None:
    with TestClient(create_app()) as client:
        # 400: missing required fields (GET and POST).
        assert client.get("/capture/add", params={"company": "", "title": ""}).status_code == 400
        assert client.post("/capture/add", json={"company": "", "title": ""}).status_code == 400

        # 200: added via GET (HTML result page).
        response = client.get(
            "/capture/add",
            params={
                "company": "Acme",
                "title": "Senior Frontend Engineer",
                "url": "https://example.com/jobs/1",
                "description": "x" * 5000,
            },
        )
        assert response.status_code == 200
        assert "Added: Acme" in response.text
        assert "back to job posting" in response.text

        # Description was truncated to 4000 chars on the stored row's cache.
        conn = connect()
        stored = list_jobs(conn)
        assert len(stored) == 1
        conn.close()

        # 409: duplicate via POST, source defaults to manual.
        response = client.post(
            "/capture/add", json={"company": "Acme", "title": "Senior Frontend Engineer"}
        )
        assert response.status_code == 409
        assert response.json()["ok"] is False

        # 200 via POST for a new job with default source.
        response = client.post(
            "/capture/add", json={"company": "Beta", "title": "Product Engineer"}
        )
        assert response.status_code == 200
        conn = connect()
        beta = next(job for job in list_jobs(conn) if job["company"] == "Beta")
        assert beta["source"] == "manual"
        conn.close()


def test_capture_endpoint_unexpected_error_is_500(
    capture_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import harrier_api.capture_routes as routes

    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("tracker exploded")

    monkeypatch.setattr(routes, "add_captured_job", boom)
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        response = client.post("/capture/add", json={"company": "A", "title": "T"})
        assert response.status_code == 500


def test_captured_description_truncated_at_4000(capture_env: Path) -> None:
    from harrier.screening.descriptions import load_cached_description

    conn = connect()
    add_captured_job(
        conn,
        company="Gamma",
        title="Frontend Engineer",
        url="https://example.com/jobs/9",
        description="y" * 5000,
    )
    cached = load_cached_description("https://example.com/jobs/9")
    assert len(cached) == 4000
