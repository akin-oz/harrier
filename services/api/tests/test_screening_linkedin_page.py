"""The posting's own page outranks the actor's data (spec 055).

The actor omits the workplace declaration in practice and text evidence
over-matches, so hybrid postings kept arriving. The one public fact left is
the job view page's JobPosting JSON-LD: jobLocationType is TELECOMMUTE
exactly when the poster tagged the job remote. Every test here fakes the
fetcher; none performs a network call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_screening import build_job, candidate_cfg

from harrier.screening.linkedin import (
    VERDICT_NOT_REMOTE,
    VERDICT_REMOTE,
    VERDICT_UNKNOWN,
    page_workplace_verdict,
    workplace_verdict_from_html,
)
from harrier.screening.normalized import NormalizedJob
from harrier.screening.pipeline import (
    LINKEDIN_PAGE_REASON,
    TrackerIndexes,
    screen_jobs,
)


def ld_page(inner_json: str) -> str:
    return f'<html><head><script type="application/ld+json">{inner_json}</script></head></html>'


# --- reading the verdict out of a page ---------------------------------------


def test_a_telecommute_declaration_reads_as_remote() -> None:
    page = ld_page('{"@type": "JobPosting", "jobLocationType": "TELECOMMUTE"}')
    assert workplace_verdict_from_html(page) == VERDICT_REMOTE


def test_the_declaration_is_matched_case_insensitively() -> None:
    page = ld_page('{"@type": "JobPosting", "jobLocationType": "Telecommute"}')
    assert workplace_verdict_from_html(page) == VERDICT_REMOTE


def test_a_job_posting_without_the_tag_reads_as_not_remote() -> None:
    """The shape every known-hybrid posting served during the measurement."""
    page = ld_page('{"@type": "JobPosting", "title": "Senior Frontend Engineer"}')
    assert workplace_verdict_from_html(page) == VERDICT_NOT_REMOTE


def test_an_unrecognized_tag_value_reads_as_not_remote() -> None:
    page = ld_page('{"@type": "JobPosting", "jobLocationType": "SOMETHING_NEW"}')
    assert workplace_verdict_from_html(page) == VERDICT_NOT_REMOTE


def test_a_list_shaped_ld_block_is_searched() -> None:
    page = ld_page(
        '[{"@type": "Organization"}, {"@type": "JobPosting", "jobLocationType": "TELECOMMUTE"}]'
    )
    assert workplace_verdict_from_html(page) == VERDICT_REMOTE


def test_a_page_with_no_job_posting_answers_nothing() -> None:
    assert workplace_verdict_from_html("<html><body>localized shape</body></html>") == (
        VERDICT_UNKNOWN
    )
    assert workplace_verdict_from_html(ld_page('{"@type": "Organization"}')) == VERDICT_UNKNOWN


def test_a_malformed_ld_block_is_skipped_not_fatal() -> None:
    page = '<script type="application/ld+json">{broken</script>' + ld_page(
        '{"@type": "JobPosting", "jobLocationType": "TELECOMMUTE"}'
    )
    assert workplace_verdict_from_html(page) == VERDICT_REMOTE


# --- the fetch, the constructed URL, and the cache ---------------------------


class CountingFetcher:
    def __init__(self, page: str) -> None:
        self.page = page
        self.urls: list[str] = []

    def __call__(self, url: str) -> str:
        self.urls.append(url)
        return self.page


@pytest.fixture
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path))
    return tmp_path


def test_the_fetched_url_is_constructed_on_the_canonical_host(isolated_data_dir: Path) -> None:
    """The posting URL is external input; the fetch target never comes from
    it, only the extracted numeric id does."""
    fetcher = CountingFetcher(ld_page('{"@type": "JobPosting"}'))
    page_workplace_verdict(
        "https://de.linkedin.com/jobs/view/senior-engineer-at-x-4434560511?trk=abc",
        fetcher=fetcher,
    )
    assert fetcher.urls == ["https://www.linkedin.com/jobs/view/4434560511"]


def test_a_url_without_a_job_id_is_unknown_and_never_fetched(isolated_data_dir: Path) -> None:
    fetcher = CountingFetcher("")
    verdict = page_workplace_verdict("https://boards.example.com/quill/305", fetcher=fetcher)
    assert verdict == VERDICT_UNKNOWN
    assert fetcher.urls == []


def test_a_failing_fetch_is_unknown(isolated_data_dir: Path) -> None:
    def explode(url: str) -> str:
        raise RuntimeError("999")

    verdict = page_workplace_verdict("https://www.linkedin.com/jobs/view/1", fetcher=explode)
    assert verdict == VERDICT_UNKNOWN


@pytest.mark.parametrize(
    ("inner", "expected"),
    [
        ('{"@type": "JobPosting", "jobLocationType": "TELECOMMUTE"}', VERDICT_REMOTE),
        ('{"@type": "JobPosting"}', VERDICT_NOT_REMOTE),
    ],
)
def test_an_answered_verdict_is_cached_and_not_refetched(
    isolated_data_dir: Path, inner: str, expected: str
) -> None:
    fetcher = CountingFetcher(ld_page(inner))
    url = "https://www.linkedin.com/jobs/view/12345"
    assert page_workplace_verdict(url, fetcher=fetcher) == expected
    assert page_workplace_verdict(url, fetcher=fetcher) == expected
    assert len(fetcher.urls) == 1


def test_a_dry_run_writes_no_verdict_cache_file(isolated_data_dir: Path) -> None:
    """Dry runs mutate nothing (the contract in discovery.py). The first
    version wrote verdict files during dry runs; caught on review of
    PR #64."""
    fetcher = CountingFetcher(ld_page('{"@type": "JobPosting"}'))
    url = "https://www.linkedin.com/jobs/view/54321"
    assert page_workplace_verdict(url, fetcher=fetcher, write_cache=False) == VERDICT_NOT_REMOTE
    assert list(isolated_data_dir.rglob("*.json")) == []
    # Nothing was remembered, so the question is asked again next time.
    assert page_workplace_verdict(url, fetcher=fetcher, write_cache=False) == VERDICT_NOT_REMOTE
    assert len(fetcher.urls) == 2


def test_a_dry_run_still_reads_an_existing_verdict_cache(isolated_data_dir: Path) -> None:
    """Read, never write: a verdict cached by a real run spares the dry run
    a fetch without the dry run leaving anything behind."""
    url = "https://www.linkedin.com/jobs/view/98765"
    warm = CountingFetcher(ld_page('{"@type": "JobPosting"}'))
    assert page_workplace_verdict(url, fetcher=warm) == VERDICT_NOT_REMOTE
    dry = CountingFetcher("")
    assert page_workplace_verdict(url, fetcher=dry, write_cache=False) == VERDICT_NOT_REMOTE
    assert dry.urls == []


def test_an_unknown_verdict_is_not_cached(isolated_data_dir: Path) -> None:
    """A later run may see a page shape that answers, so unknown is asked
    again rather than remembered."""
    fetcher = CountingFetcher("<html>no structured data</html>")
    url = "https://www.linkedin.com/jobs/view/67890"
    assert page_workplace_verdict(url, fetcher=fetcher) == VERDICT_UNKNOWN
    assert page_workplace_verdict(url, fetcher=fetcher) == VERDICT_UNKNOWN
    assert len(fetcher.urls) == 2


# --- the pipeline step -------------------------------------------------------


def _always(verdict: str) -> Any:
    def verifier(url: str) -> str:
        return verdict

    return verifier


def _screen_one(job: NormalizedJob, verifier: Any) -> Any:
    return screen_jobs(
        [job],
        candidate_cfg=candidate_cfg(),
        hold_companies=set(),
        indexes=TrackerIndexes(),
        source_seen={},
        cache_descriptions=False,
        linkedin_page_verifier=verifier,
    )


def test_a_not_remote_page_rejects_the_job_with_its_own_slug() -> None:
    job = build_job(
        location="Remote",
        remote_signal="linkedin_search",
        url="https://www.linkedin.com/jobs/view/111",
    )
    result = _screen_one(job, _always(VERDICT_NOT_REMOTE))
    assert result.new_tracker_rows == []
    assert result.rejected_counts == {LINKEDIN_PAGE_REASON: 1}


def test_a_remote_page_lets_the_job_through() -> None:
    job = build_job(
        location="Remote",
        remote_signal="linkedin_search",
        url="https://www.linkedin.com/jobs/view/222",
    )
    result = _screen_one(job, _always(VERDICT_REMOTE))
    assert len(result.new_tracker_rows) == 1
    assert result.linkedin_unverified == 0


def test_an_unknown_page_fails_open_and_is_counted() -> None:
    job = build_job(
        location="Remote",
        remote_signal="linkedin_search",
        url="https://www.linkedin.com/jobs/view/333",
    )
    result = _screen_one(job, _always(VERDICT_UNKNOWN))
    assert len(result.new_tracker_rows) == 1
    assert result.linkedin_unverified == 1


def test_a_non_linkedin_job_triggers_no_verification() -> None:
    calls: list[str] = []

    def verifier(url: str) -> str:
        calls.append(url)
        return VERDICT_NOT_REMOTE

    job = build_job(location="Remote, Europe")
    result = _screen_one(job, verifier)
    assert calls == []
    assert len(result.new_tracker_rows) == 1


def test_the_default_verifier_inherits_the_runs_cache_switch(
    isolated_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wiring, not the helper: with no injected verifier, screen_jobs
    hands page_workplace_verdict the same cache-write switch _run_source
    derives from dry_run, so a dry run cannot write through the default
    path either (review finding on PR #64)."""
    from harrier.screening import pipeline

    captured: list[bool] = []

    def fake_verdict(url: str, *, write_cache: bool = True) -> str:
        captured.append(write_cache)
        return VERDICT_REMOTE

    monkeypatch.setattr(pipeline, "page_workplace_verdict", fake_verdict)
    job = build_job(
        location="Remote",
        remote_signal="linkedin_search",
        url="https://www.linkedin.com/jobs/view/555",
    )
    screen_jobs(
        [job],
        candidate_cfg=candidate_cfg(),
        hold_companies=set(),
        indexes=TrackerIndexes(),
        source_seen={},
        cache_descriptions=False,
    )
    assert captured == [False]


def test_the_page_verdict_runs_after_the_cheap_gates() -> None:
    """A posting the title gate rejects must not cost a page fetch."""
    calls: list[str] = []

    def verifier(url: str) -> str:
        calls.append(url)
        return VERDICT_REMOTE

    job = build_job(
        title="Engineering Manager",
        location="Remote",
        remote_signal="linkedin_search",
        url="https://www.linkedin.com/jobs/view/444",
    )
    result = _screen_one(job, verifier)
    assert calls == []
    assert result.rejected_counts == {"title": 1}
