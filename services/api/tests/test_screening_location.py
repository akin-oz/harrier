"""The location gate rejects the roles it exists to find (spec 032).

Verified by running the gate, not by reading it. The board reported four
sub-claims; three reproduced and one did not, and the one that did not is
pinned here as a regression test so the working behaviour stays working.

The sharpest defect: `Remote (must be based in the EU)` was rejected as
hybrid, because "must be based" sits in the negative hints and those were
matched against the location field. That is the phrasing an EU-permit posting
uses, and the product invariant names it as a positive signal that must never
be a filter. The comment directly above the check explained the hazard and
guarded the description path only.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_screening import build_job

from harrier.screening.config import load_candidate_config
from harrier.screening.rules import (
    GATE_ORDER,
    contains_word,
    remote_region_allowed,
    split_locations,
    strip_eu_permit_phrases,
    title_allowed,
)


@pytest.fixture
def cfg() -> dict[str, Any]:
    return load_candidate_config()


def verdict(location: str, cfg: dict[str, Any], **overrides: object) -> tuple[bool, str]:
    return remote_region_allowed(build_job(location=location, **overrides), cfg)


# --- the table from the spec, allow rows and reject rows alike --------------


@pytest.mark.parametrize(
    "location",
    [
        "Remote (must be based in the EU)",
        "Remote - EU (must be based in Europe)",
        "Remote (EU work permit required)",
        "Remote - EEA (must reside in the EEA)",
        "Remote, EU residents only",
    ],
)
def test_the_eu_permit_phrasing_is_allowed(location: str, cfg: dict[str, Any]) -> None:
    """The product invariant: satisfiable through an EU entity, so a positive
    signal and never a filter. Every one of these was rejected as hybrid."""
    allowed, reason = verdict(location, cfg)
    assert allowed, f"{location} was rejected as {reason}"


@pytest.mark.parametrize(
    "location",
    ["Remote - Germany", "Remote (Portugal)", "Remote, Europe", "Remote - Spain"],
)
def test_a_remote_european_location_is_allowed(location: str, cfg: dict[str, Any]) -> None:
    """The board's second sub-claim was that these fail for a missing
    preferred region. They do not, and did not before this change. Pinned so
    the working path stays working."""
    allowed, reason = verdict(location, cfg)
    assert allowed, f"{location} was rejected as {reason}"


@pytest.mark.parametrize("location", ["Hybrid - Berlin", "On-site, Madrid", "Onsite - Lisbon"])
def test_a_non_remote_location_is_rejected(location: str, cfg: dict[str, Any]) -> None:
    allowed, reason = verdict(location, cfg)
    assert not allowed
    assert reason == "location says hybrid/on-site"


@pytest.mark.parametrize(
    "location", ["Remote - US", "Remote (United States)", "Remote, US", "Remote - Canada"]
)
def test_a_non_emea_remote_location_is_rejected(location: str, cfg: dict[str, Any]) -> None:
    """These passed and collected the region bonus, because the negative list
    had no shape matching a bare US remote posting."""
    allowed, reason = verdict(location, cfg)
    assert not allowed
    assert reason == "region looks non-EMEA"


@pytest.mark.parametrize("location", ["Remote - Jerusalem", "Remote, Siracusa, Italy"])
def test_a_city_containing_usa_is_not_read_as_non_emea(location: str, cfg: dict[str, Any]) -> None:
    """`usa` was matched unanchored, so Jerusalem and Siracusa were rejected
    as non-EMEA. Both are in scope."""
    allowed, reason = verdict(location, cfg)
    assert allowed, f"{location} was rejected as {reason}"


def test_a_multi_location_posting_qualifies_on_any_one_location(cfg: dict[str, Any]) -> None:
    """Providers join several locations into one field, and matching the whole
    field meant one out-of-scope office rejected the entire posting."""
    allowed, _ = verdict("Remote - Berlin or Remote - New York", cfg)
    assert allowed


def test_a_multi_location_posting_with_no_qualifying_location_is_rejected(
    cfg: dict[str, Any],
) -> None:
    """The other half. Allowing on any location must not allow on none."""
    allowed, reason = verdict("Remote - New York | Remote - Toronto", cfg)
    assert not allowed
    assert reason == "region looks non-EMEA"


def test_a_multi_location_posting_where_every_location_is_onsite_is_rejected(
    cfg: dict[str, Any],
) -> None:
    allowed, reason = verdict("On-site - Berlin | Hybrid - Munich", cfg)
    assert not allowed
    assert reason == "location says hybrid/on-site"


def test_a_comma_does_not_split_a_modality_from_its_place(cfg: dict[str, Any]) -> None:
    """Found while implementing. Splitting on commas evaluated "On-site" and
    "Madrid" independently, and each passed on its own: the modality carried
    no place and the place carried no modality. A comma qualifies a location;
    it does not offer an alternative."""
    assert not verdict("On-site, Madrid", cfg)[0]
    assert not verdict("Remote, US", cfg)[0]


# --- word matching, not substring -------------------------------------------


@pytest.mark.parametrize(
    ("text", "token", "expected"),
    [
        ("remote - jerusalem", "usa", False),
        ("remote, siracusa", "usa", False),
        ("remote - usa", "usa", True),
        ("reactive streams engineer", "react", False),
        ("react engineer", "react", True),
        ("kubernetes nodes", "node", False),
        ("node engineer", "node", True),
        ("senior ios engineer", "ios", True),
        ("curios engineering", "ios", False),
    ],
)
def test_matching_is_by_word(text: str, token: str, expected: bool) -> None:
    assert contains_word(text, token) is expected


def test_a_title_is_not_excluded_by_a_substring(cfg: dict[str, Any]) -> None:
    """`ios` and `qa` were matched unanchored against the title."""
    assert title_allowed("Senior Frontend Engineer, Curios Labs", cfg)
    assert not title_allowed("Senior iOS Engineer", cfg)


# --- the permit phrases are removed before any hint sees them ---------------


def test_stripping_removes_the_permit_phrase_and_leaves_the_rest() -> None:
    cleaned = strip_eu_permit_phrases("remote (must be based in the eu) berlin")
    assert "must be based" not in cleaned
    assert "berlin" in cleaned


def test_stripping_leaves_a_genuine_hybrid_marker_alone() -> None:
    """Removing the permit phrases must not remove the thing the gate is for."""
    assert "hybrid" in strip_eu_permit_phrases("hybrid (eu work permit required) berlin")


def test_a_genuine_relocation_requirement_is_still_rejected(cfg: dict[str, Any]) -> None:
    allowed, reason = verdict("Remote with relocation to Berlin required", cfg)
    assert not allowed
    assert reason == "location says hybrid/on-site"


# --- the source's own remote signal ------------------------------------------


def test_a_remote_only_board_signal_is_honoured(cfg: dict[str, Any]) -> None:
    """Only one source's signal used to be consulted, so a posting from a
    board that publishes nothing but remote roles was rejected for a missing
    remote signal."""
    allowed, reason = verdict("Anywhere", cfg, remote_signal="remote_only_board")
    assert allowed
    assert "remote_only_board" in reason


def test_the_linkedin_signal_keeps_its_own_reason(cfg: dict[str, Any]) -> None:
    allowed, reason = verdict("Remote", cfg, remote_signal="linkedin_search")
    assert allowed
    assert reason == "linkedin region-scoped search with remote evidence"


def test_the_linkedin_signal_no_longer_accepts_without_remote_evidence(
    cfg: dict[str, Any],
) -> None:
    """LinkedIn retired the query-level remote filter, so the signal stopped
    meaning "remote is query-guaranteed" (spec 053). A bare-city posting with
    no remote wording anywhere is what a leaked on-site posting looks like."""
    allowed, reason = verdict(
        "Berlin, Germany",
        cfg,
        remote_signal="linkedin_search",
        description="Ship features with a modern stack.",
    )
    assert not allowed
    assert reason == "remote signal missing"


def test_the_linkedin_signal_still_waives_the_region_text_requirement(
    cfg: dict[str, Any],
) -> None:
    """The searches stay region-scoped at query level (geoId is a live URL
    filter), so remote evidence in the posting's own text is enough: no
    preferred-region wording is required of it (specs 032, 033, 053)."""
    allowed, reason = verdict(
        "Berlin, Germany",
        cfg,
        remote_signal="linkedin_search",
        description="This is a fully remote role.",
    )
    assert allowed, f"rejected as {reason}"


def test_a_declared_hybrid_apify_item_is_rejected_end_to_end(cfg: dict[str, Any]) -> None:
    """Spec 053's acceptance row: the normalizer renders the declaration into
    the location, and the existing location gate does the rejecting."""
    from harrier.sources.apify_linkedin import normalize_apify_job

    job = normalize_apify_job(
        {
            "title": "Senior Frontend Engineer",
            "location": "Berlin, Germany",
            "workplaceTypes": ["Hybrid"],
            "descriptionText": "A remote-friendly team, on paper.",
            "link": "https://www.linkedin.com/jobs/view/1",
            "id": "1",
        }
    )
    assert job["location"] == "Hybrid, Berlin, Germany"
    allowed, reason = remote_region_allowed(job, cfg)
    assert not allowed
    assert reason == "location says hybrid/on-site"


def test_a_declared_remote_apify_item_is_accepted_end_to_end(cfg: dict[str, Any]) -> None:
    """The other half: a posting that declares remote passes by construction,
    even when its text never says the word."""
    from harrier.sources.apify_linkedin import normalize_apify_job

    job = normalize_apify_job(
        {
            "title": "Senior Frontend Engineer",
            "location": "Warsaw, Poland",
            "workplaceTypes": ["Remote"],
            "descriptionText": "Ship features with a modern stack.",
            "link": "https://www.linkedin.com/jobs/view/2",
            "id": "2",
        }
    )
    allowed, reason = remote_region_allowed(job, cfg)
    assert allowed, f"rejected as {reason}"


def test_an_apify_item_offered_remote_or_hybrid_qualifies_on_remote(
    cfg: dict[str, Any],
) -> None:
    from harrier.sources.apify_linkedin import normalize_apify_job

    job = normalize_apify_job(
        {
            "title": "Senior Frontend Engineer",
            "location": "Warsaw, Poland",
            "workplaceTypes": ["Remote", "Hybrid"],
            "link": "https://www.linkedin.com/jobs/view/3",
            "id": "3",
        }
    )
    allowed, reason = remote_region_allowed(job, cfg)
    assert allowed, f"rejected as {reason}"


def test_a_source_signal_does_not_override_a_non_remote_location(cfg: dict[str, Any]) -> None:
    """The location gate runs first on purpose: a board-wide claim about
    remoteness must not admit a posting whose own location says on-site."""
    allowed, reason = verdict("On-site - Berlin", cfg, remote_signal="remote_only_board")
    assert not allowed
    assert reason == "location says hybrid/on-site"


# --- the declared gate order -------------------------------------------------


def test_the_gate_order_is_what_the_module_declares() -> None:
    """The defect this spec fixes was a rule reading a field its own comment
    said it must not, so the order and the fields are stated in one place and
    asserted rather than left to the reader."""
    assert [reason for reason, _ in GATE_ORDER] == [
        "not remote",
        "region looks non-EMEA",
        "remote signal missing",
        "preferred region missing",
    ]

    fields_for = dict(GATE_ORDER)
    # The invariant the existing comment states and the code broke: the two
    # negative-hint gates read the location and the title, never the
    # description, because descriptions mention the words in comparisons.
    assert "description" not in fields_for["not remote"]
    assert "description" not in fields_for["region looks non-EMEA"]
    # The positive-signal gates may read it, and must, or a posting that only
    # says "remote" in its body would be rejected.
    assert "description" in fields_for["remote signal missing"]


def test_split_locations_keeps_a_single_location_intact() -> None:
    assert split_locations("Remote - Berlin") == ["Remote - Berlin"]
    assert split_locations("") == []


def test_split_locations_separates_genuine_alternatives() -> None:
    assert split_locations("Remote - Berlin | Remote - Lisbon") == [
        "Remote - Berlin",
        "Remote - Lisbon",
    ]


# --- the inert switch --------------------------------------------------------


def test_the_example_configuration_has_no_inert_remote_only_switch() -> None:
    """It read like a policy switch and was read by nothing: flipping it
    changed no decision. Remote-only is a product invariant rather than a
    setting, so the key is gone rather than wired (found on PR #33)."""
    cfg = load_candidate_config()
    candidate = cfg.get("candidate", {})
    assert isinstance(candidate, dict)
    assert "remote_only" not in candidate
