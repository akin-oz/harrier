"""Batch export and capture pins (spec 010), ported from the old repo's
tests/test_feed_importers.py and tests/test_job_server.py."""

import csv
import json
from pathlib import Path

import pytest
from conftest import TEST_TOKEN, auth
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
    """The POST contract, unchanged. The GET contract changed with spec 035
    and is covered by the tests below."""
    with TestClient(create_app()) as client:
        assert (
            client.post(
                "/capture/add", json={"company": "", "title": ""}, headers=auth()
            ).status_code
            == 400
        )

        response = client.post(
            "/capture/add",
            json={
                "company": "Acme",
                "title": "Senior Frontend Engineer",
                "url": "https://example.com/jobs/1",
                "description": "x" * 5000,
            },
            headers=auth(),
        )
        assert response.status_code == 200

        from harrier.screening.descriptions import load_cached_description

        conn = connect()
        assert len(list_jobs(conn)) == 1
        conn.close()
        assert len(load_cached_description("https://example.com/jobs/1")) == 4000

        # 409: duplicate, source defaults to manual.
        response = client.post(
            "/capture/add",
            json={"company": "Acme", "title": "Senior Frontend Engineer"},
            headers=auth(),
        )
        assert response.status_code == 409
        assert response.json()["ok"] is False

        response = client.post(
            "/capture/add", json={"company": "Beta", "title": "Product Engineer"}, headers=auth()
        )
        assert response.status_code == 200
        conn = connect()
        beta = next(job for job in list_jobs(conn) if job["company"] == "Beta")
        assert beta["source"] == "manual"
        conn.close()


def test_the_capture_get_changes_nothing(capture_env: Path) -> None:
    """The defect spec 035 closes. This route used to add the row, so
    `<img src="http://localhost:8000/capture/add?company=x&title=y">` on any
    page the operator visited wrote to the tracker with no interaction."""
    with TestClient(create_app()) as client:
        response = client.get(
            "/capture/add",
            params={"company": "Acme", "title": "Senior Frontend Engineer"},
        )
        assert response.status_code == 200
        assert "Add this posting to the tracker?" in response.text

    conn = connect()
    assert list_jobs(conn) == []
    conn.close()


def test_the_confirmation_page_carries_the_fields_and_the_token(capture_env: Path) -> None:
    with TestClient(create_app()) as client:
        response = client.get(
            "/capture/add",
            params={"company": "Acme", "title": "Senior Frontend Engineer"},
        )
    assert "Senior Frontend Engineer" in response.text
    assert 'name="token"' in response.text
    assert TEST_TOKEN in response.text


def test_the_confirmation_page_escapes_what_it_was_given(capture_env: Path) -> None:
    """The values arrive in a query string from whatever page the operator
    was reading, so they are attacker-influenced text by definition."""
    with TestClient(create_app()) as client:
        response = client.get(
            "/capture/add",
            params={"company": "<script>alert(1)</script>", "title": "T"},
        )
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_submitting_the_confirmation_form_adds_the_job(capture_env: Path) -> None:
    with TestClient(create_app()) as client:
        response = client.post(
            "/capture/add-form",
            data={
                "company": "Acme",
                "title": "Senior Frontend Engineer",
                "url": "https://example.com/jobs/1",
                "token": TEST_TOKEN,
            },
        )
        assert response.status_code == 200
        assert "Added: Acme" in response.text
        assert "back to job posting" in response.text

    conn = connect()
    assert len(list_jobs(conn)) == 1
    conn.close()


def test_the_form_without_the_token_is_refused(capture_env: Path) -> None:
    """A cross-origin page can post a form here. It cannot read the token, so
    it cannot fill that field."""
    with TestClient(create_app()) as client:
        response = client.post(
            "/capture/add-form",
            data={"company": "Acme", "title": "Senior Frontend Engineer", "token": "wrong"},
        )
        assert response.status_code == 403

    conn = connect()
    assert list_jobs(conn) == []
    conn.close()


def test_the_json_post_without_the_token_is_refused(capture_env: Path) -> None:
    with TestClient(create_app()) as client:
        response = client.post("/capture/add", json={"company": "Acme", "title": "T"})
        assert response.status_code == 403

    conn = connect()
    assert list_jobs(conn) == []
    conn.close()


def test_capture_endpoint_unexpected_error_is_500(
    capture_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import harrier_api.capture_routes as routes

    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("tracker exploded")

    monkeypatch.setattr(routes, "add_captured_job", boom)
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        response = client.post("/capture/add", json={"company": "A", "title": "T"}, headers=auth())
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


def test_read_export_rows_rejects_bad_shapes(tmp_path: Path) -> None:
    no_container = tmp_path / "bad1.json"
    no_container.write_text(json.dumps({"jobs": [{"company": "A"}]}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="no items/results container"):
        read_export_rows(no_container)

    scalar = tmp_path / "bad2.json"
    scalar.write_text(json.dumps("nope"), encoding="utf-8")
    with pytest.raises(RuntimeError, match="unsupported export payload"):
        read_export_rows(scalar)

    bad_row = tmp_path / "bad3.json"
    bad_row.write_text(json.dumps([{"company": "A"}, "not-an-object"]), encoding="utf-8")
    with pytest.raises(RuntimeError, match="row 1 is not an object"):
        read_export_rows(bad_row)

    results_container = tmp_path / "results.json"
    results_container.write_text(json.dumps({"results": [{"company": "R"}]}), encoding="utf-8")
    assert read_export_rows(results_container) == [{"company": "R"}]


def test_whitespace_source_defaults_to_manual(capture_env: Path) -> None:
    conn = connect()
    add_captured_job(conn, company="Delta", title="Engineer", source="   ")
    job = next(job for job in list_jobs(conn) if job["company"] == "Delta")
    assert job["source"] == "manual"


def test_cache_write_failure_does_not_break_capture(
    capture_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import harrier.capture as capture_module

    def boom(url: str, description: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(capture_module, "save_description_cache", boom)
    conn = connect()
    result = add_captured_job(
        conn,
        company="Epsilon",
        title="Engineer",
        url="https://example.com/jobs/50",
        description="some description",
    )
    assert result.status == "added"
    assert any(job["company"] == "Epsilon" for job in list_jobs(conn))
