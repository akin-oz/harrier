"""Behavior pins for outreach draft generation (spec 017), ported from the
old repo's tests/test_outreach_messages.py and test_generate_outreach.py
onto the synthetic outreach defaults (config/outreach-defaults.example.json,
which these tests thereby prove valid) and the committed template config."""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import harrier.outreach.drafts as drafts_module
from harrier.db import connect
from harrier.llm import LLMClientError
from harrier.outreach import (
    MESSAGE_KINDS,
    OutreachRequest,
    check_message,
    generate_message_bundle,
    generate_outreach,
    infer_audience,
    load_configs,
    parse_ai_outreach_response,
    rewrite_message,
    save_target,
    write_outreach_draft,
)
from harrier.outreach.messages import load_target_store
from harrier.profile.store import put_document

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config" / "outreach"


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    conn = connect()
    put_document(
        conn,
        "outreach_defaults",
        "defaults.json",
        "json",
        (REPO_ROOT / "config" / "outreach-defaults.example.json").read_text(encoding="utf-8"),
    )
    return conn


@pytest.fixture()
def configs(db: sqlite3.Connection) -> dict[str, Any]:
    return load_configs(db, CONFIG_DIR)


@pytest.fixture()
def request_fixture() -> OutreachRequest:
    return OutreachRequest.from_dict(
        {
            "job_post_url": "https://example.com/job",
            "company_name": "DataCamp",
            "role_title": "Senior Frontend Platform Engineer",
            "target_person_name": "Jane Recruiter",
            "audience": "recruiter",
            "tone": "direct",
            "company_notes": "The role sits close to frontend platform and shared UI work.",
            "custom_angle": (
                "The strongest overlap from my side is frontend architecture plus "
                "product-facing reliability."
            ),
        }
    )


# ---------------------------------------------------------------------------
# Template engine
# ---------------------------------------------------------------------------


def test_generate_message_bundle_returns_variants_for_each_kind(
    request_fixture: OutreachRequest, configs: dict[str, Any]
) -> None:
    bundle = generate_message_bundle(request_fixture, configs, variant_count=3)
    assert set(bundle.keys()) == set(MESSAGE_KINDS)
    for kind, variants in bundle.items():
        assert len(variants) == 3, kind
        assert all(variant.text for variant in variants)


def test_connection_note_short_respects_linkedin_limit(
    request_fixture: OutreachRequest, configs: dict[str, Any]
) -> None:
    bundle = generate_message_bundle(request_fixture, configs, variant_count=3)
    for variant in bundle["connection_note_short"]:
        assert len(variant.text) <= 300


def test_check_message_flags_generic_and_flattery_language(
    request_fixture: OutreachRequest, configs: dict[str, Any]
) -> None:
    draft = "Hi Jane, I am thrilled about this amazing company and believe I would be a great fit."
    check = check_message(draft, request_fixture, "connection_note_standard", configs)
    assert check.score < 80
    assert any("flattery" in flag or "generic" in flag or "banned" in flag for flag in check.flags)


def test_rewrite_message_removes_banned_language(
    request_fixture: OutreachRequest, configs: dict[str, Any]
) -> None:
    draft = (
        "Hi Jane, I am thrilled about this role and wanted to reach out because "
        "I think I am a perfect fit."
    )
    rewritten = rewrite_message(draft, request_fixture, "connection_note_short", configs)
    lowered = rewritten.lower()
    assert "thrilled" not in lowered
    assert "perfect fit" not in lowered
    assert len(rewritten) <= 300


def test_save_target_upserts_by_identity(
    db: sqlite3.Connection, request_fixture: OutreachRequest
) -> None:
    save_target(request_fixture)
    warm = OutreachRequest.from_dict(
        {
            "job_post_url": "https://example.com/job",
            "company_name": "DataCamp",
            "role_title": "Senior Frontend Platform Engineer",
            "target_person_name": "Jane Recruiter",
            "audience": "recruiter",
            "tone": "warm",
        }
    )
    save_target(warm)
    stored = load_target_store()
    assert len(stored) == 1
    assert stored[0]["tone"] == "warm"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def test_infer_audience_distinguishes_recruiter_hiring_manager_and_peer() -> None:
    assert infer_audience("Talent Partner") == "recruiter"
    assert infer_audience("Engineering Manager") == "hiring_manager"
    assert infer_audience("Senior Frontend Engineer") == "peer"
    assert infer_audience("") == "recruiter"


def test_generate_outreach_returns_sections_and_primary_messages(
    db: sqlite3.Connection,
) -> None:
    payload = generate_outreach(
        db,
        company="Veriff",
        role="Senior Frontend Engineer",
        job_url="",
        contact_name="Fernanda",
        contact_role="Talent Partner",
        contact_linkedin="https://linkedin.com/in/fernanda",
        jd_text="Frontend role with TypeScript, performance, and shared UI work.",
        config_dir=CONFIG_DIR,
    )
    assert "messages" in payload
    selected = payload["selected_messages"]
    assert isinstance(selected, dict)
    from typing import cast

    picked = cast("dict[str, str]", selected)
    assert picked["connection_note_short"].startswith("Hi Fernanda")
    messages = cast("dict[str, list[dict[str, object]]]", payload["messages"])
    first_short = messages["connection_note_short"][0]
    assert int(str(first_short["char_count"])) <= 300
    assert payload["recruiter_message"] == selected["connection_note_standard"]
    assert payload["follow_up_message"] == selected["follow_up_after_application_first"]


def test_write_outreach_draft_renders_sections(db: sqlite3.Connection) -> None:
    payload = generate_outreach(
        db,
        company="Veriff",
        role="Senior Frontend Engineer",
        contact_name="Fernanda",
        contact_role="Talent Partner",
        jd_text="Frontend role with TypeScript.",
        config_dir=CONFIG_DIR,
    )
    paths = write_outreach_draft("Veriff", "Senior Frontend Engineer", payload)
    assert paths["json"].exists()
    markdown = paths["markdown"].read_text(encoding="utf-8")
    assert "# LinkedIn Connection Note (Under 300)" in markdown
    assert "## Alternative v2" in markdown


def test_staff_titles_resolve_before_broad_frontend(configs: dict[str, Any]) -> None:
    from harrier.outreach.messages import resolve_role_profile

    profile = resolve_role_profile("Staff Frontend Engineer", configs["role_profiles"])
    assert profile.get("key") == "web_staff"
    broad = resolve_role_profile("Senior Frontend Engineer", configs["role_profiles"])
    assert broad.get("key") == "frontend_engineer"


def test_optional_sentences_keep_a_separator(
    configs: dict[str, Any], request_fixture: OutreachRequest
) -> None:
    bundle = generate_message_bundle(request_fixture, configs, variant_count=3)
    for variants in bundle.values():
        for variant in variants:
            assert ".Happy" not in variant.text
            assert (
                not any(
                    left.isalnum() and right.isalnum() and left == "."
                    for left, right in zip(variant.text, variant.text[1:], strict=False)
                )
                or "." not in variant.text
            )


def test_supplied_jd_changes_deterministic_output(db: sqlite3.Connection) -> None:
    # The hiring-manager standard note consumes the company-note sentence,
    # which the resolved JD now feeds (review finding: the JD was loaded
    # but never reached the template request).
    without_jd = generate_outreach(
        db,
        company="Veriff",
        role="Senior Frontend Engineer",
        contact_name="Miguel",
        contact_role="Engineering Manager",
        jd_text="",
        config_dir=CONFIG_DIR,
    )
    with_jd = generate_outreach(
        db,
        company="Veriff",
        role="Senior Frontend Engineer",
        contact_name="Miguel",
        contact_role="Engineering Manager",
        jd_text="A verification platform with document scanning flows.",
        config_dir=CONFIG_DIR,
    )
    assert json.dumps(without_jd["messages"]) != json.dumps(with_jd["messages"])
    assert "verification platform" in json.dumps(with_jd["messages"]).lower()


def test_drafts_for_two_contacts_write_distinct_artifacts(db: sqlite3.Connection) -> None:
    paths_by_contact: list[Path] = []
    for name in ("Fernanda", "Miguel"):
        payload = generate_outreach(
            db,
            company="Veriff",
            role="Senior Frontend Engineer",
            contact_name=name,
            contact_role="Talent Partner",
            jd_text="Frontend role with TypeScript.",
            config_dir=CONFIG_DIR,
        )
        paths = write_outreach_draft("Veriff", "Senior Frontend Engineer", payload)
        paths_by_contact.append(paths["json"])
    assert paths_by_contact[0] != paths_by_contact[1]
    assert all(path.exists() for path in paths_by_contact)


# ---------------------------------------------------------------------------
# AI path
# ---------------------------------------------------------------------------


def ai_kind(texts: list[str]) -> list[dict[str, str]]:
    return [{"variant_id": f"v{i + 1}", "text": text} for i, text in enumerate(texts)]


def full_ai_response(short_text: str = "Short note about DataCamp frontend work.") -> str:
    return json.dumps(
        {
            "connection_note_short": ai_kind([short_text, "b", "c"]),
            "connection_note_standard": ai_kind(["a", "b", "c"]),
            "follow_up_after_connection": ai_kind(["a", "b", "c"]),
            "follow_up_after_application_first": ai_kind(["a", "b", "c"]),
            "follow_up_after_application_second": ai_kind(["a", "b", "c"]),
        }
    )


def test_parse_ai_response_rejects_missing_kind() -> None:
    incomplete_payload = {
        kind: ai_kind(["a", "b", "c"])
        for kind in MESSAGE_KINDS
        if kind != "follow_up_after_application_second"
    }
    with pytest.raises(ValueError, match="missing or empty message kind"):
        parse_ai_outreach_response(json.dumps(incomplete_payload))


def test_parse_ai_response_rejects_one_variant_or_blank_text() -> None:
    single = json.dumps({kind: ai_kind(["only one"]) for kind in MESSAGE_KINDS})
    with pytest.raises(ValueError, match="need 3"):
        parse_ai_outreach_response(single)
    blank = json.dumps({kind: ai_kind(["a", "  ", "c"]) for kind in MESSAGE_KINDS})
    with pytest.raises(ValueError, match="invalid variant"):
        parse_ai_outreach_response(blank)


def test_parse_ai_response_hard_trims_short_notes() -> None:
    long_text = "word " * 100
    parsed = parse_ai_outreach_response(full_ai_response(long_text.strip()))
    assert len(parsed["connection_note_short"][0]["text"]) <= 280


def test_ai_outreach_propagates_ai_error(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    put_document(db, "resume_truth", "truth.md", "markdown", "- Verified fact.\n")
    put_document(
        db,
        "candidate",
        "candidate.json",
        "json",
        json.dumps({"candidate": {"name": "Deniz Örnek"}}),
    )
    put_document(
        db,
        "application_profile",
        "application-profile.json",
        "json",
        (REPO_ROOT / "config" / "application-profile.example.json").read_text(encoding="utf-8"),
    )
    put_document(
        db,
        "application_profile",
        "application-profile.md",
        "markdown",
        (REPO_ROOT / "config" / "application-profile.example.md").read_text(encoding="utf-8"),
    )

    def boom(system_prompt: str, user_input: str) -> str:
        raise LLMClientError("backend unavailable")

    monkeypatch.setattr(drafts_module, "generate_text", boom)
    with pytest.raises(RuntimeError, match="AI request failed"):
        generate_outreach(
            db,
            company="Veriff",
            role="Senior Frontend Engineer",
            contact_role="Talent Partner",
            ai=True,
        )
