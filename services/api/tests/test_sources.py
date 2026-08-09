"""Source normalization behavior pins, ported from the old repo's
tests/test_feed_importers.py (spec 008). Fixtures are the old synthetic ones."""

import json
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from harrier.screening.normalized import NormalizedJob
from harrier.sources import ashby as ashby_module
from harrier.sources import fetch_many
from harrier.sources import lever as lever_module
from harrier.sources.ashby import fetch_ashby_jobs
from harrier.sources.feeds import parse_ats_feeds
from harrier.sources.greenhouse import normalize_greenhouse_job
from harrier.sources.lever import fetch_lever_jobs

FIXTURES = Path(__file__).parent / "fixtures" / "job_discovery"


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_greenhouse_normalization() -> None:
    payload = json.loads(load_fixture("greenhouse_jobs.json"))
    job = normalize_greenhouse_job(payload["jobs"][0], "https://boards.greenhouse.io/exampleco")
    assert job["source"] == "greenhouse"
    assert job["external_id"] == "101"
    assert job["company"] == "exampleco"


def test_ashby_normalization() -> None:
    payload = json.loads(load_fixture("ashby_jobs_api.json"))
    with patch.object(ashby_module, "request_json", return_value=payload):
        jobs = fetch_ashby_jobs("https://jobs.ashbyhq.com/exampleco")
    assert len(jobs) == 1
    assert jobs[0]["source"] == "ashby"
    assert jobs[0]["company"] == "ExampleCo"
    assert jobs[0]["raw_compensation"] == "EUR year 90000 115000"


def test_ashby_falls_back_to_html_when_api_404s() -> None:
    with (
        patch.object(
            ashby_module,
            "request_json",
            side_effect=HTTPError(
                "https://api.ashbyhq.com/posting-api/job-board/exampleco",
                404,
                "Not Found",
                hdrs=None,  # pyright: ignore[reportArgumentType]
                fp=None,
            ),
        ),
        patch.object(ashby_module, "request_text", return_value=load_fixture("ashby_appdata.html")),
    ):
        jobs = fetch_ashby_jobs("https://jobs.ashbyhq.com/exampleco")
    assert len(jobs) == 1
    assert jobs[0]["source"] == "ashby"
    assert jobs[0]["company"] == "ExampleCo"


def test_lever_normalization() -> None:
    payload = json.loads(load_fixture("lever_jobs.json"))
    with patch.object(lever_module, "request_json", return_value=payload):
        jobs = fetch_lever_jobs("https://jobs.lever.co/exampleco")
    assert len(jobs) == 1
    assert jobs[0]["source"] == "lever"
    assert jobs[0]["external_id"] == "lever-1"


def test_lever_uses_eu_api_base_for_eu_board() -> None:
    payload = json.loads(load_fixture("lever_jobs.json"))
    with patch.object(lever_module, "request_json", return_value=payload) as request_json_mock:
        fetch_lever_jobs("https://jobs.eu.lever.co/exampleco")
    assert "https://api.eu.lever.co/v0/postings/exampleco" in request_json_mock.call_args.args[0]


def test_fetch_many_isolates_a_failing_board() -> None:
    def fetch(board_url: str) -> list[NormalizedJob]:
        if "badco" in board_url:
            raise RuntimeError("tls timeout")
        return []

    jobs, errors = fetch_many(
        ["https://boards.greenhouse.io/badco", "https://boards.greenhouse.io/goodco"],
        fetch,
        "greenhouse",
    )
    assert jobs == []
    assert errors == ["https://boards.greenhouse.io/badco: tls timeout"]


def test_parse_ats_feeds_routes_by_netloc(tmp_path: Path) -> None:
    feeds = tmp_path / "feeds.txt"
    feeds.write_text(
        "# comment line\n"
        "https://boards.greenhouse.io/exampleco\n"
        "https://jobs.ashbyhq.com/exampleco\n"
        "https://jobs.lever.co/exampleco\n"
        "https://jobs.eu.lever.co/euco\n"
        "https://unknown.example.com/board\n",
        encoding="utf-8",
    )
    grouped = parse_ats_feeds(feeds)
    assert grouped["greenhouse"] == ["https://boards.greenhouse.io/exampleco"]
    assert grouped["ashby"] == ["https://jobs.ashbyhq.com/exampleco"]
    assert grouped["lever"] == [
        "https://jobs.lever.co/exampleco",
        "https://jobs.eu.lever.co/euco",
    ]
