"""Apify LinkedIn importer and guest-endpoint pins (spec 009), ported from the
old repo's tests/test_import_apify_linkedin_jobs.py plus new helper pins."""

import json
from pathlib import Path

import pytest

from harrier.screening.descriptions import cache_job_descriptions, load_cached_description
from harrier.screening.linkedin import (
    extract_poster_from_html,
    extract_publisher_contact,
    linkedin_job_id,
)
from harrier.sources.apify_linkedin import (
    DEFAULT_COUNT,
    actor_input,
    load_dataset_files,
    normalize_apify_job,
    unwrap_apify_data,
)


def test_default_count_is_150() -> None:
    assert DEFAULT_COUNT == 150


def test_actor_input_uses_requested_count_and_scrape_company_flag() -> None:
    payload = actor_input(
        ["https://www.linkedin.com/jobs/search/?keywords=Senior%20Frontend%20Engineer"],
        count=25,
        scrape_company=False,
    )
    assert payload["count"] == 25
    assert payload["scrapeCompany"] is False
    assert payload["splitByLocation"] is False


def test_unwrap_apify_data_handles_wrapped_run_payload() -> None:
    payload = {"data": {"id": "run-123", "defaultDatasetId": "dataset-123", "status": "RUNNING"}}
    unwrapped = unwrap_apify_data(payload)
    assert isinstance(unwrapped, dict)
    assert unwrapped["id"] == "run-123"
    assert unwrapped["defaultDatasetId"] == "dataset-123"


def test_normalize_apify_job_maps_fields() -> None:
    job = normalize_apify_job(
        {
            "Title": "Senior Frontend Engineer",
            "Description": "Remote Europe role using React and TypeScript.",
            "Detail URL": "https://www.linkedin.com/jobs/view/123",
            "Location": "Remote, Europe",
            "Company Name": "ExampleCo",
            "Created At": "2026-03-20T12:00:00.000Z",
            "jobId": "123",
        }
    )
    assert job["source"] == "apify_linkedin"
    assert job["company"] == "ExampleCo"
    assert job["title"] == "Senior Frontend Engineer"
    assert job["url"] == "https://www.linkedin.com/jobs/view/123"
    assert job["external_job_id"] == "123"
    assert job["remote_signal"] == "linkedin_search"


# --- the workplace declaration rides in the location (spec 053) -------------


@pytest.mark.parametrize("declared", ["Hybrid", "On-site", "Onsite", "HYBRID"])
def test_a_declared_non_remote_type_is_prefixed_to_the_location(declared: str) -> None:
    job = normalize_apify_job(
        {"title": "T", "location": "Berlin, Germany", "workplaceTypes": [declared], "id": "1"}
    )
    expected = "On-site" if declared.lower() in {"on-site", "onsite"} else "Hybrid"
    assert job["location"] == f"{expected}, Berlin, Germany"


def test_a_declared_remote_type_is_prefixed_to_the_location() -> None:
    job = normalize_apify_job(
        {"title": "T", "location": "Warsaw, Poland", "workplaceTypes": ["Remote"], "id": "1"}
    )
    assert job["location"] == "Remote, Warsaw, Poland"


def test_multiple_declared_types_join_as_alternatives() -> None:
    job = normalize_apify_job(
        {
            "title": "T",
            "location": "Warsaw, Poland",
            "workplaceTypes": ["Remote", "Hybrid"],
            "id": "1",
        }
    )
    assert job["location"] == "Remote | Hybrid, Warsaw, Poland"


def test_work_remote_allowed_true_counts_only_without_a_declared_type() -> None:
    fallback = normalize_apify_job(
        {"title": "T", "location": "Lisbon, Portugal", "workRemoteAllowed": True, "id": "1"}
    )
    assert fallback["location"] == "Remote, Lisbon, Portugal"
    outranked = normalize_apify_job(
        {
            "title": "T",
            "location": "Lisbon, Portugal",
            "workplaceTypes": ["Hybrid"],
            "workRemoteAllowed": True,
            "id": "1",
        }
    )
    assert outranked["location"] == "Hybrid, Lisbon, Portugal"


def test_work_remote_allowed_false_is_not_a_declaration() -> None:
    """False is what the scraper reports for hybrid, on-site, and missing
    data alike, so it cannot name which one."""
    job = normalize_apify_job(
        {"title": "T", "location": "Berlin, Germany", "workRemoteAllowed": False, "id": "1"}
    )
    assert job["location"] == "Berlin, Germany"


@pytest.mark.parametrize("raw", [["Flexible"], [2], "somewhere nice", {"type": "Remote"}, [], None])
def test_an_unrecognized_or_malformed_declaration_changes_nothing(raw: object) -> None:
    job = normalize_apify_job(
        {"title": "T", "location": "Berlin, Germany", "workplaceTypes": raw, "id": "1"}
    )
    assert job["location"] == "Berlin, Germany"


def test_a_declaration_with_no_location_stands_alone() -> None:
    job = normalize_apify_job({"title": "T", "workplaceTypes": ["Hybrid"], "id": "1"})
    assert job["location"] == "Hybrid"


def test_a_string_declaration_is_treated_as_one_entry() -> None:
    job = normalize_apify_job(
        {"title": "T", "location": "Berlin, Germany", "workplaceTypes": "Remote", "id": "1"}
    )
    assert job["location"] == "Remote, Berlin, Germany"


def test_normalize_apify_job_maps_lowercase_field_shape() -> None:
    job = normalize_apify_job(
        {
            "title": "Senior Frontend Engineer",
            "descriptionText": "Remote Europe role.",
            "link": "https://www.linkedin.com/jobs/view/456",
            "location": "Remote",
            "companyName": "OtherCo",
            "id": "456",
        }
    )
    assert job["company"] == "OtherCo"
    assert job["url"] == "https://www.linkedin.com/jobs/view/456"
    assert job["external_id"] == "456"


def test_load_dataset_files_reads_local_apify_export(tmp_path: Path) -> None:
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(
        json.dumps(
            [
                {
                    "title": "Senior Frontend Engineer",
                    "companyName": "ExampleCo",
                    "location": "Remote, Europe",
                    "descriptionText": "Remote Europe role.",
                    "link": "https://www.linkedin.com/jobs/view/123",
                    "id": "123",
                }
            ]
        ),
        encoding="utf-8",
    )
    items = load_dataset_files([str(dataset_path)])
    assert len(items) == 1
    assert items[0]["companyName"] == "ExampleCo"


def test_load_dataset_files_unwraps_items_container(tmp_path: Path) -> None:
    dataset_path = tmp_path / "wrapped.json"
    dataset_path.write_text(
        json.dumps({"data": {"items": [{"title": "T", "id": "1"}]}}), encoding="utf-8"
    )
    items = load_dataset_files([str(dataset_path)])
    assert len(items) == 1


def test_linkedin_job_id_handles_both_url_shapes() -> None:
    assert linkedin_job_id("https://www.linkedin.com/jobs/view/1234567890") == "1234567890"
    assert (
        linkedin_job_id(
            "https://www.linkedin.com/jobs/view/senior-frontend-engineer-at-exampleco-1234567890"
        )
        == "1234567890"
    )
    assert linkedin_job_id("https://www.linkedin.com/jobs/search/?currentJobId=987654") == "987654"
    assert linkedin_job_id("https://example.com/not-linkedin") == ""
    assert linkedin_job_id("") == ""


def test_extract_publisher_contact_flat_and_nested() -> None:
    flat = extract_publisher_contact(
        {"posterFullName": "Sam Sample", "posterProfileUrl": "https://linkedin.com/in/sam"}
    )
    assert flat == {
        "name": "Sam Sample",
        "linkedin_url": "https://linkedin.com/in/sam",
        "title": "",
    }

    nested = extract_publisher_contact(
        {
            "poster": {
                "name": "Ada Example",
                "profileUrl": "https://linkedin.com/in/ada",
                "title": "Recruiter",
            }
        }
    )
    assert nested["name"] == "Ada Example"
    assert nested["title"] == "Recruiter"

    assert extract_publisher_contact({"Title": "no poster info"}) == {}


GUEST_HTML = """
<html><body>
<section class="message-the-recruiter core-section-container">
  <a class="base-card__full-link" href="https://www.linkedin.com/in/sam-sample?trk=x">
    <h3 class="base-main-card__title">Sam &amp; Sample</h3>
  </a>
  <h4 class="base-main-card__subtitle">Talent Partner</h4>
</section>
<div class="show-more-less-html__markup">Remote Europe role with React.</div>
</body></html>
"""


def test_extract_poster_from_html_reads_recruiter_section() -> None:
    poster = extract_poster_from_html(GUEST_HTML)
    assert poster["name"] == "Sam & Sample"
    assert poster["linkedin_url"] == "https://www.linkedin.com/in/sam-sample"
    assert poster["title"] == "Talent Partner"


def test_extract_poster_requires_person_profile_link() -> None:
    html = GUEST_HTML.replace("/in/sam-sample", "/company/exampleco")
    assert extract_poster_from_html(html) == {}


def test_cache_job_descriptions_caches_everything_fetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path))
    jobs = [
        normalize_apify_job(
            {
                "title": "Senior Frontend Engineer",
                "description": "Remote Europe role.",
                "link": "https://www.linkedin.com/jobs/view/1",
                "id": "1",
            }
        ),
        normalize_apify_job(
            {"title": "No description", "link": "https://www.linkedin.com/jobs/view/2", "id": "2"}
        ),
    ]
    cached = cache_job_descriptions(jobs)
    assert cached == 1
    assert load_cached_description("https://www.linkedin.com/jobs/view/1") == "Remote Europe role."
    assert load_cached_description("https://www.linkedin.com/jobs/view/2") == ""
