"""A missing description is fetched from the posting (spec 151).

The two readers harrier already has, the LinkedIn guest endpoint and the
allow-listed ATS hosts, fill a job's missing description: one job from the
Apply panel or `harrier description fetch`, every job from the backfill.
Every request here is stubbed; nothing reaches the network.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from conftest import auth
from fastapi.testclient import TestClient

import harrier.apply.description as description_module
from harrier.apply.description import BACKFILL_PAUSE_SECONDS, backfill_descriptions
from harrier.db import connect
from harrier.screening.descriptions import load_cached_description, save_description_cache
from harrier.screening.http import DisallowedUrlError
from harrier.screening.linkedin import GUEST_JD_URL
from harrier.tracker.store import add_job
from harrier.tracks import add_track, default_scope, resolve_scope
from harrier_api.app import create_app
from harrier_cli.main import main

LINKEDIN_URL = "https://www.linkedin.com/jobs/view/4000000001"
GREENHOUSE_URL = "https://boards.greenhouse.io/exampleco/jobs/123"
# Invented text, long enough to count as a description (spec 151's 120).
POSTING = (
    "Exampleco is hiring a remote frontend engineer to own its design system, "
    "work with product and design, and ship accessible interfaces in React."
)
GUEST_PAGE = (
    '<html><div class="show-more-less-html__markup show-more-less-html__markup--clamp">'
    f"<p>{POSTING}</p></div></html>"
)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    return tmp_path


@pytest.fixture
def client(env: Path) -> TestClient:
    return TestClient(create_app())


def _add(url: str, *, status: str = "prospect", company: str = "Exampleco", track: str = "") -> int:
    conn = connect()
    try:
        scope = resolve_scope(conn, track) if track else default_scope(conn)
        return add_job(
            conn,
            {
                "company": company,
                "title": "Senior Frontend Engineer",
                "url": url,
                "source": "apify_linkedin",
                "location": "Remote, Europe",
                "status": status,
            },
            scope=scope,
        )
    finally:
        conn.close()


def _no_wait(seconds: float) -> None:
    """A pause that does not wait."""


def _no_request(*args: object, **kwargs: object) -> str:
    raise AssertionError("a request was made")


# --- one job ---


def test_a_linkedin_description_is_fetched_and_stored(
    env: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _add(LINKEDIN_URL)
    asked: list[str] = []

    def guest(url: str, **kwargs: Any) -> str:
        asked.append(url)
        assert kwargs["retries"] == 1
        return GUEST_PAGE

    monkeypatch.setattr("harrier.screening.linkedin.request_text", guest)
    response = client.post(f"/apply/{job}/description/fetch", headers=auth())

    assert response.status_code == 200
    assert response.json() == {"text": POSTING, "stored": True}
    assert asked == [GUEST_JD_URL.format(job_id="4000000001")]
    assert load_cached_description(LINKEDIN_URL) == POSTING
    read = client.get(f"/apply/{job}/description", headers=auth())
    assert read.json() == {"text": POSTING, "stored": True}


def test_an_ats_description_is_fetched_and_a_bad_redirect_is_refused(
    env: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _add(GREENHOUSE_URL)
    calls: list[dict[str, Any]] = []

    def ats(url: str, **kwargs: Any) -> str:
        calls.append({"url": url, **kwargs})
        return f"<html><nav>Jobs</nav><main>{POSTING}</main></html>"

    monkeypatch.setattr(description_module, "request_text", ats)
    response = client.post(f"/apply/{job}/description/fetch", headers=auth())
    assert response.status_code == 200
    assert POSTING in load_cached_description(GREENHOUSE_URL)
    # Every redirect is checked against the enrichment allow-list.
    assert calls[0]["url_allowed"] is description_module.enrich_url_allowed

    other = _add("https://jobs.lever.co/exampleco/abc", company="Otherco")

    def redirected(url: str, **kwargs: Any) -> str:
        raise DisallowedUrlError(f"redirect to disallowed URL blocked: {url}")

    monkeypatch.setattr(description_module, "request_text", redirected)
    refused = client.post(f"/apply/{other}/description/fetch", headers=auth())
    assert refused.status_code == 502
    assert refused.json() == {"detail": "the posting page could not be read (DisallowedUrlError)"}
    assert load_cached_description("https://jobs.lever.co/exampleco/abc") == ""


@pytest.mark.parametrize(
    ("url", "stored", "status", "detail"),
    [
        (
            "https://remoteok.com/remote-jobs/123",
            False,
            409,
            "no description fetcher for remoteok.com",
        ),
        # The LinkedIn id pattern on another host is not a LinkedIn job.
        (
            "https://example.com/jobs/view/4000000001",
            False,
            409,
            "no description fetcher for example.com",
        ),
        (LINKEDIN_URL, True, 409, "a description is already stored for job {job}"),
        ("", False, 409, "job {job} has no URL; a description is stored by URL"),
    ],
)
def test_a_fetch_is_refused_before_any_request(
    env: Path,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    stored: bool,
    status: int,
    detail: str,
) -> None:
    job = _add(url)
    if stored:
        save_description_cache(url, POSTING)
    monkeypatch.setattr("harrier.screening.linkedin.request_text", _no_request)
    monkeypatch.setattr(description_module, "request_text", _no_request)
    response = client.post(f"/apply/{job}/description/fetch", headers=auth())
    assert response.status_code == status
    assert response.json() == {"detail": detail.format(job=job)}


@pytest.mark.parametrize(
    ("page", "detail"),
    [
        (
            '<div class="show-more-less-html__markup">Apply now.</div>',
            "the posting page held no description",
        ),
        (
            RuntimeError(f"HTTP 429 for {GUEST_JD_URL.format(job_id='4000000001')}"),
            "the posting page could not be read (RuntimeError)",
        ),
    ],
)
def test_an_empty_or_failed_page_stores_nothing(
    env: Path,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    page: str | Exception,
    detail: str,
) -> None:
    job = _add(LINKEDIN_URL)

    def guest(url: str, **kwargs: Any) -> str:
        if isinstance(page, Exception):
            raise page
        return page

    monkeypatch.setattr("harrier.screening.linkedin.request_text", guest)
    response = client.post(f"/apply/{job}/description/fetch", headers=auth())
    assert response.status_code == 502
    # The exact sentence: it never names the posting's URL.
    assert response.json() == {"detail": detail}
    assert load_cached_description(LINKEDIN_URL) == ""


def test_the_fetch_route_and_verb_reach_one_function(env: Path, client: TestClient) -> None:
    job = _add(LINKEDIN_URL)
    with patch("harrier.apply.description.fetch_description", return_value=POSTING) as domain:
        assert main(["description", "fetch", str(job)]) == 0
        client.post(f"/apply/{job}/description/fetch", headers=auth())
    cli_call, api_call = domain.call_args_list
    assert cli_call.args == api_call.args == (job, LINKEDIN_URL)


def test_the_fetch_verb_reports_a_refusal(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    job = _add("https://remoteok.com/remote-jobs/123")
    assert main(["description", "fetch", str(job)]) == 1
    assert "description failed: no description fetcher for remoteok.com" in capsys.readouterr().err


# --- every job ---


def _readers(monkeypatch: pytest.MonkeyPatch, failing: set[str] | None = None) -> list[str]:
    """Stub both readers; return the URLs each was asked for, in order."""
    asked: list[str] = []

    def read(url: str) -> str:
        asked.append(url)
        if failing and url in failing:
            raise RuntimeError(f"HTTP 503 for {url}")
        return POSTING

    monkeypatch.setattr(description_module, "_linkedin_text", read)
    monkeypatch.setattr(description_module, "_ats_text", read)
    return asked


def _account(env: Path) -> dict[str, int]:
    """One job of each kind the backfill must tell apart."""
    jobs = {
        "rejected": _add("https://www.linkedin.com/jobs/view/4000000010", status="rejected"),
        "no_url": _add("", company="Manualco"),
        "stored": _add(GREENHOUSE_URL, company="Storedco"),
        "no_fetcher": _add("https://remoteok.com/remote-jobs/9", company="Remoteco"),
        "linkedin": _add(LINKEDIN_URL, company="Linkco"),
        "lever": _add("https://jobs.lever.co/exampleco/abc", company="Leverco"),
    }
    save_description_cache(GREENHOUSE_URL, POSTING)
    conn = connect()
    try:
        add_track(conn, "lab-search", "academic", "Lab search")
    finally:
        conn.close()
    # A row on another track: the backfill walks every track.
    jobs["other_track"] = _add(
        "https://www.linkedin.com/jobs/view/4000000020", company="Labco", track="lab-search"
    )
    return jobs


def test_the_backfill_fetches_only_what_is_missing(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    jobs = _account(env)
    asked = _readers(monkeypatch)
    monkeypatch.setattr(description_module.time, "sleep", _no_wait)

    # A dry run requests nothing and stores nothing.
    assert main(["description", "backfill", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert asked == []
    assert "would fetch: 3" in out
    assert load_cached_description(LINKEDIN_URL) == ""

    # --limit caps the fetches.
    assert main(["description", "backfill", "--limit", "1"]) == 0
    assert asked == [LINKEDIN_URL]
    capsys.readouterr()

    # The rest, with one page failing: the run still exits 0.
    asked.clear()
    failing_url = "https://jobs.lever.co/exampleco/abc"
    _readers(monkeypatch, failing={failing_url})
    assert main(["description", "backfill"]) == 0
    out = capsys.readouterr().out
    assert f"job {jobs['lever']}: the posting page could not be read (RuntimeError)" in out
    assert f"job {jobs['other_track']}: fetched" in out
    for line in (
        "fetched: 1",
        "already stored: 2",
        "no URL: 1",
        "no fetcher for the host: 1",
        "fetch failed: 1",
        "skipped as rejected: 1",
    ):
        assert line in out, line
    assert load_cached_description("https://www.linkedin.com/jobs/view/4000000020") == POSTING
    assert load_cached_description("https://www.linkedin.com/jobs/view/4000000010") == ""


def test_the_backfill_pauses_between_requests_to_one_host(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add(LINKEDIN_URL)
    _add("https://jobs.lever.co/exampleco/abc", company="Leverco")
    _add("https://www.linkedin.com/jobs/view/4000000020", company="Labco")
    _readers(monkeypatch)
    now = [100.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    conn = connect()
    try:
        summary = backfill_descriptions(conn, sleep=sleep, clock=lambda: now[0])
    finally:
        conn.close()
    assert summary.fetched == 3
    # LinkedIn, Lever, LinkedIn: one pause, before the second LinkedIn request.
    assert slept == [BACKFILL_PAUSE_SECONDS]


def test_a_fetch_logs_no_posting_identity(
    env: Path,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker_url = "https://www.linkedin.com/jobs/view/4000000077"
    job = _add(marker_url, company="Markerco")

    def guest(url: str, **kwargs: Any) -> str:
        raise RuntimeError(f"HTTP 503 for {url}")

    monkeypatch.setattr("harrier.screening.linkedin.request_text", guest)
    with caplog.at_level(logging.DEBUG):
        client.post(f"/apply/{job}/description/fetch", headers=auth())
        conn = connect()
        try:
            backfill_descriptions(conn, sleep=_no_wait)
        finally:
            conn.close()
    assert "4000000077" not in caplog.text
    assert "Markerco" not in caplog.text
