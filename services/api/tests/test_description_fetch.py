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
        # A redirect off LinkedIn is refused (spec 151 review).
        assert kwargs["url_allowed"](url)
        assert not kwargs["url_allowed"]("https://evil.example/jobs/view/4000000001")
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
    # Every redirect is checked: an allow-listed host and the posting's path.
    rule = calls[0]["url_allowed"]
    assert rule(GREENHOUSE_URL)
    assert not rule("https://boards.greenhouse.io/exampleco")
    assert not rule("https://evil.example/exampleco/jobs/123")

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
            "https://careers.example.org/openings/123",
            False,
            409,
            "no description fetcher for careers.example.org",
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
    job = _add("https://careers.example.org/openings/123")
    assert main(["description", "fetch", str(job)]) == 1
    assert (
        "description failed: no description fetcher for careers.example.org"
        in capsys.readouterr().err
    )


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
        "no_fetcher": _add("https://careers.example.org/openings/9", company="Careersco"),
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


# --- the review of PR #392 ---


def test_a_slugged_linkedin_url_fetches_its_own_posting(
    env: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _add("https://www.linkedin.com/jobs/view/senior-engineer-at-3m-3912345678")
    asked: list[str] = []

    def guest(url: str, **kwargs: Any) -> str:
        asked.append(url)
        return GUEST_PAGE

    monkeypatch.setattr("harrier.screening.linkedin.request_text", guest)
    assert client.post(f"/apply/{job}/description/fetch", headers=auth()).status_code == 200
    assert asked == [GUEST_JD_URL.format(job_id="3912345678")]


def test_an_ats_redirect_must_keep_the_posting_path() -> None:
    allowed = description_module.same_posting(GREENHOUSE_URL)
    assert allowed(GREENHOUSE_URL)
    assert allowed("https://job-boards.greenhouse.io/exampleco/jobs/123/")
    # A closed posting's redirect to its board's listing is refused.
    assert not allowed("https://boards.greenhouse.io/exampleco")
    assert not allowed("https://evil.example/exampleco/jobs/123")


def test_an_unreadable_store_is_never_read_as_empty(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fetch that cannot read the store stops, rather than writing over a
    pasted description it could not see."""
    _add(LINKEDIN_URL)

    class Unreadable:
        def read(self, key: str) -> bytes | None:
            raise OSError("the store did not answer")

    monkeypatch.setattr("harrier.screening.descriptions.files", Unreadable)
    monkeypatch.setattr("harrier.screening.linkedin.request_text", _no_request)
    with pytest.raises(OSError):
        description_module.fetch_description(1, LINKEDIN_URL)


def test_the_backfill_pauses_for_every_linkedin_subdomain(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every LinkedIn job is read from the one guest endpoint, so the pause
    is per endpoint, not per subdomain the job's URL names."""
    _add("https://www.linkedin.com/jobs/view/4000000031", company="Aco")
    _add("https://uk.linkedin.com/jobs/view/4000000032", company="Bco")
    _add("https://linkedin.com/jobs/view/4000000033", company="Cco")
    _readers(monkeypatch)
    now = [100.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    conn = connect()
    try:
        backfill_descriptions(conn, sleep=sleep, clock=lambda: now[0])
    finally:
        conn.close()
    assert slept == [BACKFILL_PAUSE_SECONDS, BACKFILL_PAUSE_SECONDS]


def test_a_description_stored_mid_run_is_counted_and_the_run_goes_on(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    first = _add(LINKEDIN_URL, company="Aco")
    second = _add("https://www.linkedin.com/jobs/view/4000000020", company="Bco")
    _readers(monkeypatch)
    monkeypatch.setattr(description_module.time, "sleep", _no_wait)
    real = description_module.read_description_strictly

    def raced(url: str) -> str:
        # Discovery stored the first job's description after the walk looked.
        return POSTING if url == LINKEDIN_URL else real(url)

    monkeypatch.setattr(description_module, "read_description_strictly", raced)
    assert main(["description", "backfill"]) == 0
    out = capsys.readouterr().out
    assert f"job {first}: already stored" in out
    assert f"job {second}: fetched" in out
    assert "already stored: 1" in out


def test_a_store_error_stops_the_backfill(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _add(LINKEDIN_URL)
    _readers(monkeypatch)

    def refused(*args: object) -> str:
        raise OSError("Storage refused the write for descriptions/abc.json")

    monkeypatch.setattr(description_module, "store_description", refused)
    assert main(["description", "backfill"]) == 1
    err = capsys.readouterr().err
    assert err.strip() == "description backfill stopped: OSError"


def test_a_negative_backfill_limit_is_a_usage_error(env: Path) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["description", "backfill", "--limit", "-5"])
    assert raised.value.code == 2


# --- the test-integrity review of PR #392 ---


@pytest.mark.parametrize(
    ("body", "dry_run", "limit"),
    [({}, True, None), ({"dry_run": False, "limit": 2}, False, 2), ({"dry_run": True}, True, None)],
)
def test_the_backfill_route_builds_the_run_it_was_asked_for(
    env: Path, body: dict[str, Any], dry_run: bool, limit: int | None
) -> None:
    """The Operations page's two buttons reach the CLI as a dry run and as a
    fetch: the route's body decides the run's argv."""
    from harrier_api.runs import Run, RunManager, build_command

    manager = RunManager(journal_path=env / "data" / "runs" / "journal.jsonl")
    argv: list[list[str]] = []

    async def start(kind: str, params: Any = None, **_: Any) -> Run:
        argv.append(build_command(kind, params)[3:])
        return Run(id="recorded", kind=kind, command=[])

    manager.start = start  # type: ignore[method-assign]
    client = TestClient(create_app(run_manager=manager))
    response = client.post("/ops/descriptions/backfill", json=body, headers=auth())
    assert response.status_code == 200
    expected = ["description", "backfill"]
    if dry_run:
        expected.append("--dry-run")
    if limit is not None:
        expected.append(f"--limit={limit}")
    assert argv == [expected]


def test_the_backfill_pauses_after_a_failed_request_too(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add(LINKEDIN_URL, company="Aco")
    _add("https://www.linkedin.com/jobs/view/4000000020", company="Bco")
    _readers(monkeypatch, failing={LINKEDIN_URL})
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
    assert summary.failed == 1 and summary.fetched == 1
    assert slept == [BACKFILL_PAUSE_SECONDS]


def test_the_backfill_waits_only_what_is_left_of_the_pause(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pause runs from the end of one request to the start of the next:
    time the walk spends between them counts against it."""
    _add(LINKEDIN_URL, company="Aco")
    _add("https://www.linkedin.com/jobs/view/4000000020", company="Bco")
    _readers(monkeypatch)
    now = [100.0]
    real = description_module.stored_description

    def slow_check(url: str) -> str:
        now[0] += 1.5  # the walk's own check takes a second and a half
        return real(url)

    monkeypatch.setattr(description_module, "stored_description", slow_check)
    slept: list[float] = []
    conn = connect()
    try:
        backfill_descriptions(conn, sleep=slept.append, clock=lambda: now[0])
    finally:
        conn.close()
    assert slept == [pytest.approx(BACKFILL_PAUSE_SECONDS - 1.5)]


def test_every_backfill_line_names_a_job_by_id_only(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import re

    jobs = _account(env)
    _readers(monkeypatch, failing={"https://jobs.lever.co/exampleco/abc"})
    monkeypatch.setattr(description_module.time, "sleep", _no_wait)
    assert main(["description", "backfill"]) == 0
    out = capsys.readouterr().out
    job_lines = [line for line in out.splitlines() if line.startswith("job ")]
    assert job_lines
    for line in job_lines:
        assert re.fullmatch(r"job \d+: [a-z ,()A-Za-z]+", line), line
    for identity in ("linkedin.com", "lever.co", "Linkco", "Leverco", "Labco", "Frontend"):
        assert identity not in out, identity
    assert str(jobs["linkedin"]) in out


def test_a_dry_run_honours_the_limit(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _account(env)
    asked = _readers(monkeypatch)
    assert main(["description", "backfill", "--dry-run", "--limit", "1"]) == 0
    assert "would fetch: 1" in capsys.readouterr().out
    assert asked == []


def test_a_fetched_page_is_cut_at_its_limit_and_a_short_one_refused(
    env: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_job = _add(LINKEDIN_URL)
    long_text = "x" * 10_000

    def long_page(url: str, **kwargs: Any) -> str:
        return f'<div class="show-more-less-html__markup">{long_text}</div>'

    monkeypatch.setattr("harrier.screening.linkedin.request_text", long_page)
    assert client.post(f"/apply/{long_job}/description/fetch", headers=auth()).status_code == 200
    assert len(load_cached_description(LINKEDIN_URL)) == description_module.FETCHED_TEXT_LIMIT

    lever_url = "https://jobs.lever.co/exampleco/abc"
    long_ats = _add(lever_url, company="Longco")

    def long_ats_page(url: str, **kwargs: Any) -> str:
        return f"<html>{long_text}</html>"

    monkeypatch.setattr(description_module, "request_text", long_ats_page)
    assert client.post(f"/apply/{long_ats}/description/fetch", headers=auth()).status_code == 200
    assert len(load_cached_description(lever_url)) == description_module.FETCHED_TEXT_LIMIT

    short_job = _add(GREENHOUSE_URL, company="Shortco")
    just_short = "x" * (description_module.MIN_DESCRIPTION_LENGTH_FOR_SCORING - 1)

    def short_page(url: str, **kwargs: Any) -> str:
        return f"<html>{just_short}</html>"

    monkeypatch.setattr(description_module, "request_text", short_page)
    refused = client.post(f"/apply/{short_job}/description/fetch", headers=auth())
    assert refused.json() == {"detail": "the posting page held no description"}
    assert load_cached_description(GREENHOUSE_URL) == ""
