"""Screening behavior pins, ported from the old repo's tests/test_job_sources.py
(spec 007). Fixture values are the old suite's synthetic ones."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import pytest

from harrier.screening import (
    TrackerIndexes,
    dedupe_normalized_jobs,
    make_normalized_job,
    remote_region_allowed,
    score_job,
    screen_jobs,
)
from harrier.screening import http as screening_http
from harrier.screening.normalized import NormalizedJob
from harrier.screening.pipeline import build_tracker_indexes


def candidate_cfg() -> dict[str, Any]:
    return {
        "candidate": {
            "preferred_regions": ["europe", "emea"],
            "preferred_countries": [],
            "remote_only": True,
        },
        "targets": {
            "titles": [
                "Senior Product Engineer",
                "Senior Frontend Engineer",
                "Senior Software Engineer",
            ],
            "title_keywords_include": [
                "frontend",
                "product engineer",
                "typescript",
                "vue",
                "react",
                "nuxt",
            ],
            "title_keywords_exclude": [
                "manager",
                "engineering manager",
                "head of",
                "director",
                "onsite only",
            ],
        },
        "scoring": {
            "base_score": 30,
            "exact_title_bonus": 20,
            "include_keyword_bonus": 6,
            "include_keyword_bonus_cap": 18,
            "remote_bonus": 10,
            "preferred_region_bonus": 8,
            "skill_signals": {
                "typescript": 7,
                "vue": 7,
                "nuxt": 6,
                "react": 7,
                "frontend": 5,
                "product engineer": 6,
                "node": 4,
            },
            "preferred_signal_weights": {
                "ownership": 4,
                "testing": 4,
                "ci/cd": 4,
                "architectural influence": 4,
                "strong engineering culture": 3,
                "observability": 4,
                "performance": 4,
            },
        },
    }


def build_job(**overrides: object) -> NormalizedJob:
    job = make_normalized_job(
        source="greenhouse",
        company="TestCo",
        title="Senior Frontend Engineer",
        location="Remote, Europe",
        url="https://example.com/jobs/1",
        description=(
            "TypeScript React ownership testing ci/cd strong engineering culture remote Europe."
        ),
        created_at="2026-03-20T00:00:00Z",
        external_id="gh-1",
        board_key="testco",
    )
    job.update(overrides)  # type: ignore[typeddict-item]  # pyright: ignore[reportArgumentType, reportCallIssue]
    return job


def _screen(jobs: list[NormalizedJob], cfg: dict[str, Any], **kwargs: object):
    defaults: dict[str, Any] = {
        "candidate_cfg": cfg,
        "hold_companies": set(),
        "indexes": TrackerIndexes(),
        "source_seen": set(),
        "cache_descriptions": False,
    }
    defaults.update(kwargs)
    return screen_jobs(jobs, **defaults)


def test_score_job_respects_configured_scoring_overrides() -> None:
    cfg = candidate_cfg()
    cfg["targets"]["titles"].append("Senior Frontend Platform Engineer")
    cfg["targets"]["title_keywords_include"].append("platform")
    cfg["scoring"]["exact_title_bonus"] = 28
    cfg["scoring"]["include_keyword_bonus"] = 8
    cfg["scoring"]["skill_signals"]["platform"] = 6
    score, reasons = score_job(
        build_job(
            title="Senior Frontend Platform Engineer",
            location="Portugal",
            description="Frontend platform work for TypeScript teams.",
        ),
        cfg,
    )
    assert score >= 70
    assert "exact target title" in reasons


def test_enrichment_fetches_job_page_for_thin_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harrier.screening.descriptions import enrich_job_description_for_scoring

    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path))
    job = build_job(
        source="hiring_cafe",
        title="Senior Frontend Platform Engineer",
        location="Portugal",
        url="https://job-boards.greenhouse.io/datacamp/jobs/7741105",
        description="",
    )
    html = """
    <html><body>
    <h1>Senior Frontend Platform Engineer</h1>
    <p>React TypeScript design system developer experience microfrontend architecture
    performance observability Storybook Playwright ownership.</p>
    </body></html>
    """
    with patch.object(screening_http, "request_text", return_value=html):
        enriched = enrich_job_description_for_scoring(job)

    assert "React TypeScript design system developer experience" in enriched["description"]


def test_screen_jobs_enriches_before_low_score_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path))
    thin_job = build_job(
        source="hiring_cafe",
        title="Senior Frontend Platform Engineer",
        location="Remote, Portugal",
        url="https://job-boards.greenhouse.io/datacamp/jobs/7741105",
        description="",
    )
    html = """
    <html><body>
    <h1>Senior Frontend Platform Engineer</h1>
    <p>Remote Portugal role for a Senior Frontend Platform Engineer.</p>
    <p>React TypeScript design system developer experience microfrontend architecture
    performance observability Storybook Playwright ownership testing CI/CD accessibility
    component library frontend architecture.</p>
    </body></html>
    """
    cfg = candidate_cfg()
    cfg["candidate"]["preferred_countries"] = ["portugal"]
    with patch.object(screening_http, "request_text", return_value=html):
        result = _screen([thin_job], cfg)

    assert len(result.new_tracker_rows) == 1
    assert int(result.new_tracker_rows[0]["fit_score"]) >= 60


def test_hybrid_rejection() -> None:
    allowed, reason = remote_region_allowed(build_job(location="Hybrid, Berlin"), candidate_cfg())
    assert allowed is False
    assert reason == "location says hybrid/on-site"


def test_home_office_location_is_not_a_hybrid_false_positive() -> None:
    """The 'office' hint was removed after matching 'Remote (Home Office)'."""
    allowed, _ = remote_region_allowed(
        build_job(location="Remote (Home Office), Europe"), candidate_cfg()
    )
    assert allowed is True


def test_flex_remote_is_not_a_false_positive() -> None:
    allowed, _ = remote_region_allowed(build_job(location="Flex remote, Europe"), candidate_cfg())
    assert allowed is True


def test_non_emea_rejection() -> None:
    allowed, reason = remote_region_allowed(
        build_job(location="Remote, United States"), candidate_cfg()
    )
    assert allowed is False
    assert reason == "region looks non-EMEA"


def test_us_offices_in_description_do_not_reject_emea_role() -> None:
    """Region negatives check title+location only, never the description."""
    allowed, _ = remote_region_allowed(
        build_job(
            location="Remote, Europe",
            description="We have offices in New York and San Francisco. Remote Europe role.",
        ),
        candidate_cfg(),
    )
    assert allowed is True


def test_linkedin_search_bypasses_region_check() -> None:
    allowed, reason = remote_region_allowed(
        build_job(location="Remote", remote_signal="linkedin_search"),
        candidate_cfg(),
    )
    assert allowed is True
    assert reason == "linkedin remote-filtered search result"


def test_eu_permit_phrases_raise_score_never_reject() -> None:
    # A minimal description so the 120 cap is not already reached.
    base_score, _ = score_job(build_job(description="Remote Europe role."), candidate_cfg())
    boosted_score, _ = score_job(
        build_job(description="Remote Europe role. EU work permit required, eu entity only."),
        candidate_cfg(),
    )
    assert boosted_score > base_score
    allowed, _ = remote_region_allowed(
        build_job(location="Remote, Europe. EU work permit required."), candidate_cfg()
    )
    assert allowed is True


def test_dedupe_prefers_external_id_or_url() -> None:
    unique = dedupe_normalized_jobs(
        [
            build_job(external_id="abc", url="https://example.com/1"),
            build_job(external_id="abc", url="https://example.com/2"),
            build_job(external_id="", url="https://example.com/3"),
            build_job(external_id="", url="https://example.com/3"),
        ]
    )
    assert len(unique) == 2
    assert unique[0]["external_id"] == "abc"
    assert unique[1]["url"] == "https://example.com/3"


def test_tracker_duplicate_rejected_across_sources_by_url() -> None:
    indexes = TrackerIndexes(urls={"https://example.com/jobs/1"})
    result = _screen(
        [build_job(source="lever", external_id="lever-1")],
        candidate_cfg(),
        indexes=indexes,
    )
    assert result.new_tracker_rows == []
    assert result.rejected_counts["tracker_duplicate"] == 1


def test_hold_list_rejects_before_anything_else() -> None:
    result = _screen([build_job()], candidate_cfg(), hold_companies={"testco"})
    assert result.new_tracker_rows == []
    assert result.rejected_counts["hold"] == 1
    assert result.skipped_hold == 1


def test_build_tracker_indexes_reads_external_key_from_column_or_notes() -> None:
    rows = [
        {
            "company": "TestCo",
            "title": "Senior Frontend Engineer",
            "url": "https://example.com/jobs/1",
            "external_key": "",
            "notes": "external_key=greenhouse:gh-1; source_label=greenhouse:testco",
        },
        {
            "company": "Beta",
            "title": "Product Engineer",
            "url": "https://example.com/jobs/2",
            "external_key": "lever:lv-9",
            "notes": "",
        },
    ]
    indexes = build_tracker_indexes(rows)
    assert "https://example.com/jobs/1" in indexes.urls
    assert ("testco", "senior frontend engineer") in indexes.company_title
    assert "greenhouse:gh-1" in indexes.external_keys
    assert "lever:lv-9" in indexes.external_keys


def test_accepted_row_notes_carry_the_legacy_key_values() -> None:
    result = _screen([build_job()], candidate_cfg())
    assert len(result.new_tracker_rows) == 1
    notes = result.new_tracker_rows[0]["notes"]
    assert "score=" in notes
    assert "archetype=" in notes
    assert "remote_filter=pass" in notes
    assert "external_key=greenhouse:gh-1" in notes


def test_request_text_retries_and_eventually_succeeds() -> None:
    response = MagicMock()
    response.read.return_value = b'{"ok": true}'
    response.__enter__.return_value = response
    response.__exit__.return_value = False

    with (
        patch.object(
            screening_http,
            "urlopen",
            side_effect=[URLError(TimeoutError("handshake timed out")), response],
        ) as urlopen_mock,
        patch.object(screening_http.time, "sleep") as sleep_mock,
    ):
        body = screening_http.request_text("https://example.com/jobs.json", retries=2)

    assert body == '{"ok": true}'
    assert urlopen_mock.call_count == 2
    sleep_mock.assert_called_once_with(2)


def test_request_text_raises_runtime_error_after_retries() -> None:
    with (
        patch.object(
            screening_http,
            "urlopen",
            side_effect=URLError(TimeoutError("handshake timed out")),
        ),
        patch.object(screening_http.time, "sleep"),
        pytest.raises(RuntimeError, match="HTTP request failed after 2 attempts"),
    ):
        screening_http.request_text("https://example.com/jobs.json", retries=2)


def test_enrich_url_allowed_blocks_spoofed_hosts() -> None:
    from harrier.screening.descriptions import enrich_url_allowed

    assert enrich_url_allowed("https://job-boards.greenhouse.io/acme/jobs/1") is True
    assert enrich_url_allowed("https://jobs.lever.co/acme/1") is True
    assert enrich_url_allowed("https://greenhouse.io/acme") is True
    # Userinfo trick: the hint is in userinfo, the host is an internal IP.
    assert enrich_url_allowed("https://greenhouse.io@169.254.169.254/") is False
    # Suffix spoof: approved name embedded in an attacker domain.
    assert enrich_url_allowed("https://greenhouse.io.attacker.example/") is False
    assert enrich_url_allowed("ftp://greenhouse.io/x") is False
    assert enrich_url_allowed("https://user:pass@greenhouse.io/") is False
    assert enrich_url_allowed("") is False


def test_should_enrich_rejects_spoofed_urls() -> None:
    from harrier.screening.descriptions import should_enrich_description_for_scoring

    assert (
        should_enrich_description_for_scoring(
            build_job(url="https://greenhouse.io@169.254.169.254/", description="")
        )
        is False
    )


def test_redirect_to_disallowed_host_is_blocked() -> None:
    from email.message import Message
    from urllib.request import Request

    from harrier.screening.descriptions import enrich_url_allowed
    from harrier.screening.http import DisallowedUrlError, ValidatingRedirectHandler

    handler = ValidatingRedirectHandler(enrich_url_allowed)
    request = Request("https://greenhouse.io/jobs/1")
    with pytest.raises(DisallowedUrlError, match="redirect to disallowed URL"):
        handler.redirect_request(
            request, MagicMock(), 302, "Found", Message(), "http://169.254.169.254/latest"
        )


def test_request_text_refuses_disallowed_initial_url() -> None:
    from harrier.screening.descriptions import enrich_url_allowed
    from harrier.screening.http import DisallowedUrlError

    with pytest.raises(DisallowedUrlError, match="request to disallowed URL"):
        screening_http.request_text(
            "https://greenhouse.io.attacker.example/", url_allowed=enrich_url_allowed
        )


def test_low_score_enrichment_is_cached_and_not_refetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path))
    # A thin job whose enriched description still scores below the cutoff.
    thin_job = build_job(
        title="Senior Frontend Engineer",
        location="Remote, Europe",
        url="https://job-boards.greenhouse.io/acme/jobs/9",
        description="",
    )
    html = "<html><body><p>Remote Europe role. Nothing else relevant here.</p></body></html>"
    cfg = candidate_cfg()
    cfg["scoring"]["base_score"] = 0
    cfg["scoring"]["exact_title_bonus"] = 0
    cfg["scoring"]["include_keyword_bonus"] = 0
    cfg["scoring"]["remote_bonus"] = 0
    cfg["scoring"]["preferred_region_bonus"] = 0
    cfg["scoring"]["skill_signals"] = {}
    cfg["scoring"]["preferred_signal_weights"] = {}

    with patch.object(screening_http, "request_text", return_value=html) as fetch:
        first = _screen([thin_job], cfg, cache_descriptions=True)
        assert first.rejected_counts.get("low_score") == 1
        assert fetch.call_count == 1

        # Same URL screened again (fresh seen-state): served from the cache.
        again = build_job(
            title="Senior Frontend Engineer",
            location="Remote, Europe",
            url="https://job-boards.greenhouse.io/acme/jobs/9",
            description="",
        )
        second = _screen([again], cfg, cache_descriptions=True)
        assert second.rejected_counts.get("low_score") == 1
        assert fetch.call_count == 1


def test_hybrid_wording_in_description_does_not_reject() -> None:
    """Negative hints are location-only by design: descriptions use the words
    in comparisons without describing the role's own policy."""
    allowed, _ = remote_region_allowed(
        build_job(
            location="Remote, Europe",
            description="Unlike hybrid roles, we are fully remote across Europe.",
        ),
        candidate_cfg(),
    )
    assert allowed is True


def test_must_be_based_in_eu_description_stays_accepted() -> None:
    """'must be based' is a negative hint, but 'must be based in EU' in a
    description is an explicitly positive signal (product invariant); the
    location-only scoping of negative hints is what keeps this true."""
    job = build_job(
        location="Remote, Europe",
        description="Fully remote. You must be based in EU for this role.",
    )
    allowed, _ = remote_region_allowed(job, candidate_cfg())
    assert allowed is True
    minimal_cfg = candidate_cfg()
    base, _ = score_job(build_job(description="Remote Europe role."), minimal_cfg)
    with_signal, _ = score_job(
        build_job(description="Remote Europe role. Based in EU contractors welcome."),
        minimal_cfg,
    )
    assert with_signal > base


def test_malformed_url_is_rejected_without_raising() -> None:
    from harrier.screening.descriptions import (
        enrich_url_allowed,
        should_enrich_description_for_scoring,
    )

    assert enrich_url_allowed("https://[") is False
    assert (
        should_enrich_description_for_scoring(build_job(url="https://[", description="")) is False
    )
