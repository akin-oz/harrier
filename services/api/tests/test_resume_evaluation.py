"""Spec 070: the fit evaluation rates each posting requirement on the
evidence that names it, and every gap becomes a question.

Runs on the synthetic bundle (config/resume-facts.example.json and
config/resume-framing.example.json) and the
Weflow posting fixture, which is real job data with the founders' names
removed. No candidate content appears here.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import cast

import pytest
from resume_support import example_bundle_raw

from harrier.resume import ResumeBundle, parse_bundle
from harrier.resume.evaluation import (
    CONFIDENCE,
    classify_posting,
    evaluate_resume_fit,
    extract_jd_requirements,
    format_fit_evaluation_markdown,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
WEFLOW = Path(__file__).parent / "fixtures" / "resume" / "weflow-senior-principal.txt"
WEFLOW_ROLE = "Senior/Principal Software Engineer (m/f/d) - remote"
AS_OF = date(2026, 8, 1)


@pytest.fixture()
def bundle() -> ResumeBundle:
    return parse_bundle(example_bundle_raw())


@pytest.fixture()
def weflow(bundle: ResumeBundle) -> dict[str, object]:
    return evaluate_resume_fit(bundle, WEFLOW.read_text(encoding="utf-8"), WEFLOW_ROLE, as_of=AS_OF)


def matrix(evaluation: dict[str, object]) -> list[dict[str, object]]:
    return cast("list[dict[str, object]]", evaluation["evidence_matrix"])


def row(evaluation: dict[str, object], requirement: str) -> dict[str, object]:
    return next(item for item in matrix(evaluation) if item["requirement"] == requirement)


def requirements(bundle: ResumeBundle, jd: str) -> list[str]:
    return [item["requirement"] for item in extract_jd_requirements(bundle, jd)]


def status_of(bundle: ResumeBundle, jd: str, role: str = "Engineer") -> str:
    evaluation = evaluate_resume_fit(bundle, jd, role, as_of=AS_OF)
    (only,) = matrix(evaluation)
    return str(only["evidence_status"])


# --- extraction (X1 to X7) ---


def test_section_headers_are_never_rows(bundle: ResumeBundle) -> None:
    jd = "Requirements\n- Experience with React and TypeScript.\nTasks\n- Own the frontend.\n"
    rows = requirements(bundle, jd)
    assert "Requirements" not in rows
    assert "Tasks" not in rows
    assert rows


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("- Experience with PostgreSQL in production.", "requirement"),
        ("- Build integrations with partner systems.", "responsibility"),
        ("- Annual company off-site with all expenses paid.", "benefit"),
        ("Example Co automates data capture for sales teams.", "company_context"),
    ],
)
def test_classifier_labels_requirement_responsibility_benefit_and_context(
    line: str, expected: str
) -> None:
    sections = {
        "requirement": "Requirements",
        "responsibility": "Tasks",
        "benefit": "Benefits",
        "company_context": "About us",
    }
    jd = f"{sections[expected]}\n{line}\n"
    items = classify_posting(jd)
    assert [item.item_class for item in items] == [expected]


def test_text_before_the_first_header_is_company_context() -> None:
    jd = "Example Co is backed by investors.\nRequirements\n- Experience with Python services.\n"
    classes = [item.item_class for item in classify_posting(jd)]
    assert classes == ["company_context", "requirement"]


def test_invitation_and_eeo_lines_are_company_context() -> None:
    jd = (
        "Tasks\n"
        "You will build integrations for customers. If you enjoy hard problems: let's talk!\n"
        "Benefits\n"
        "- Paid leave every year.\n"
        "Example Co is an equal opportunity employer. All applicants receive consideration.\n"
    )
    items = {item.text: item.item_class for item in classify_posting(jd)}
    assert items["You will build integrations for customers."] == "responsibility"
    assert items["If you enjoy hard problems: let's talk!"] == "company_context"
    assert items["Example Co is an equal opportunity employer."] == "company_context"
    assert items["All applicants receive consideration."] == "company_context"
    assert items["Paid leave every year."] == "benefit"


def test_posting_without_headers_falls_back_to_requirements(bundle: ResumeBundle) -> None:
    jd = "Experience with React is required. Strong software architecture skills."
    assert requirements(bundle, jd) == [
        "Experience with React is required.",
        "Strong software architecture skills.",
    ]


def test_compound_technology_line_splits_into_sub_requirements(bundle: ResumeBundle) -> None:
    jd = (
        "Requirements\n"
        "- Expert-level proficiency in React + Typescript / Next.js, Node.js, PostgreSQL.\n"
    )
    rows = extract_jd_requirements(bundle, jd)
    assert [item["requirement"] for item in rows] == [
        "Expert-level proficiency in React",
        "Expert-level proficiency in Typescript",
        "Expert-level proficiency in Next.js",
        "Expert-level proficiency in Node.js",
        "Expert-level proficiency in PostgreSQL",
    ]
    assert all(item["source"].startswith("Expert-level proficiency in React +") for item in rows)


def test_compound_split_keeps_a_remainder_that_names_a_concept(bundle: ResumeBundle) -> None:
    jd = "Own the architecture of a React and TypeScript product."
    rows = requirements(bundle, jd)
    assert "Own the architecture of a React" in rows
    assert "Own the architecture of a TypeScript" in rows
    assert "Own the architecture of a product." in rows


def test_duplicate_requirement_lines_give_one_row(bundle: ResumeBundle) -> None:
    jd = (
        "Requirements\n- Experience with PostgreSQL.\n- experience with  PostgreSQL\n"
        "Tasks\n- Experience with PostgreSQL.\n"
    )
    assert requirements(bundle, jd) == ["Experience with PostgreSQL."]


def test_importance_core_from_must_and_first_three(bundle: ResumeBundle) -> None:
    jd = (
        "Requirements\n"
        "- First listed requirement about Python.\n"
        "- Second listed requirement about Kafka.\n"
        "- Third listed requirement about Redis.\n"
        "- Fourth listed requirement about Docker.\n"
        "- You must know Terraform well.\n"
        "- Kubernetes is a plus.\n"
        "Nice to have\n"
        "- Experience with Rust services.\n"
        "Tasks\n"
        "- Build integrations with partner systems.\n"
    )
    importance = {
        item["requirement"]: item["jd_importance"] for item in extract_jd_requirements(bundle, jd)
    }
    assert importance["First listed requirement about Python."] == "core"
    assert importance["Third listed requirement about Redis."] == "core"
    assert importance["Fourth listed requirement about Docker."] == "important"
    assert importance["You must know Terraform well."] == "core"
    assert importance["Kubernetes is a plus."] == "nice-to-have"
    assert importance["Experience with Rust services."] == "nice-to-have"
    assert importance["Build integrations with partner systems."] == "important"


def test_compensation_lines_are_not_matrix_rows(bundle: ResumeBundle) -> None:
    assert requirements(bundle, "Compensation range is required.") == []


# --- evidence rating (R1 to R9) ---


@pytest.mark.parametrize(
    ("jd", "expected"),
    [
        # R1: a bullet names the technology.
        ("Experience with Storybook.", "Direct"),
        # R2: only a member of the family is named.
        ("Experience with AWS.", "Partial"),
        # R3: some of the named terms, not all.
        ("Experience with Storybook in agile teams.", "Partial"),
        # R4: years against the computed career length (12 on AS_OF).
        ("10+ years of experience in software development.", "Direct"),
        ("15+ years of experience in software development.", "Unsupported"),
        # R5: a concept term the bullet shows.
        ("Strong software architecture skills.", "Direct"),
        # R6: the right dimension, but no bullet names the term.
        ("Experience with css at depth.", "Adjacent"),
        # R7: nothing.
        ("Experience with PostgreSQL.", "Unsupported"),
    ],
)
def test_status_rules(bundle: ResumeBundle, jd: str, expected: str) -> None:
    assert status_of(bundle, jd) == expected


def test_aws_lambda_is_partial_for_aws(bundle: ResumeBundle) -> None:
    evaluation = evaluate_resume_fit(bundle, "Experience with AWS.", as_of=AS_OF)
    only = matrix(evaluation)[0]
    assert only["evidence_status"] == "Partial"
    reasons = [entry["reason"] for entry in cast("list[dict[str, str]]", only["exact_cv_evidence"])]
    assert any("AWS Lambda, one part of AWS" in reason for reason in reasons)


def test_dimension_evidence_without_the_term_is_adjacent(bundle: ResumeBundle) -> None:
    # "css" is a frontend signal; no bullet says css, though the frontend
    # dimension cites three bullets. Topical is Adjacent at most.
    evaluation = evaluate_resume_fit(bundle, "Experience with css at depth.", as_of=AS_OF)
    only = matrix(evaluation)[0]
    assert only["evidence_status"] == "Adjacent"
    evidence = cast("list[dict[str, str]]", only["exact_cv_evidence"])
    assert evidence
    assert all("does not name css" in entry["reason"] for entry in evidence)


def test_backend_mention_is_capped_without_a_backend_role(bundle: ResumeBundle) -> None:
    assert status_of(bundle, "Strong backend skills.") == "Partial"


def test_absent_by_default_terms_are_never_covered(bundle: ResumeBundle) -> None:
    # React is named by a bullet; "game" belongs to an absent-by-default
    # dimension, so the row cannot be Direct.
    assert status_of(bundle, "Build game interfaces with React.") == "Partial"


def test_a_bullet_naming_an_absent_by_default_term_still_does_not_cover_it() -> None:
    # The operator marks a dimension absent by default when the CV must not
    # claim it without confirmation, even if a bullet uses the word.
    raw = example_bundle_raw()
    raw["bullet_pool"]["r1_b6"] = "Added AI tooling to the release pipeline."
    bundle = parse_bundle(raw)
    evaluation = evaluate_resume_fit(bundle, "Hands-on AI experience.", as_of=AS_OF)
    assert matrix(evaluation)[0]["evidence_status"] == "Unsupported"


@pytest.mark.parametrize(
    ("bullet", "jd"),
    [
        ("Wrote lambda functions in a functional style.", "Experience with AWS."),
        ("Added guard rails to every deploy.", "Experience with Ruby on Rails."),
    ],
)
def test_an_ordinary_word_does_not_name_a_technology(bullet: str, jd: str) -> None:
    raw = example_bundle_raw()
    # Only the posting vocabulary is under test, so the bundle's own aliases
    # for these technologies are taken out.
    raw["technology_aliases"]["AWS Lambda"] = ["aws lambda"]
    raw["bullet_pool"]["r1_b6"] = bullet
    bundle = parse_bundle(raw)
    (only,) = matrix(evaluate_resume_fit(bundle, jd, as_of=AS_OF))
    cited = [entry["ref"] for entry in cast("list[dict[str, str]]", only["exact_cv_evidence"])]
    assert "r1_b6" not in cited


@pytest.mark.parametrize(
    "jd",
    [
        "Experience with Storybook.",
        "Experience with AWS.",
        "Experience with css at depth.",
        "Experience with PostgreSQL.",
    ],
)
def test_confidence_follows_status(bundle: ResumeBundle, jd: str) -> None:
    only = matrix(evaluate_resume_fit(bundle, jd, as_of=AS_OF))[0]
    assert only["confidence"] == CONFIDENCE[str(only["evidence_status"])]


def test_every_evidence_entry_has_a_one_line_reason(weflow: dict[str, object]) -> None:
    for item in matrix(weflow):
        for entry in cast("list[dict[str, str]]", item["exact_cv_evidence"]):
            assert entry["reason"].strip()
            assert "\n" not in entry["reason"]


# --- gaps and questions (G1 to G5) ---


def test_section_four_lists_every_non_direct_row(weflow: dict[str, object]) -> None:
    non_direct = [item for item in matrix(weflow) if item["evidence_status"] != "Direct"]
    gaps = cast("list[dict[str, object]]", weflow["unsupported_or_partial_requirements"])
    assert {str(item["requirement"]) for item in gaps} == {
        str(item["requirement"]) for item in non_direct
    }
    order = ["Partial", "Adjacent", "Unsupported"]
    statuses = [order.index(str(item["evidence_status"])) for item in gaps]
    assert statuses == sorted(statuses)


def test_section_four_says_so_when_everything_is_direct(bundle: ResumeBundle) -> None:
    evaluation = evaluate_resume_fit(bundle, "Experience with Storybook.", as_of=AS_OF)
    report = format_fit_evaluation_markdown(evaluation, "Example Co", "Engineer")
    assert "- Every requirement has Direct evidence." in report


def test_one_question_per_gap_row(bundle: ResumeBundle) -> None:
    jd = (
        "Requirements\n"
        "- Experience with PostgreSQL.\n"
        "- Experience with AWS.\n"
        "- Solid understanding of design patterns.\n"
        "- Overlap with EU working hours (CET +/- 2 hours).\n"
        "- Experience with Storybook.\n"
    )
    questions = cast(
        "list[str]", evaluate_resume_fit(bundle, jd, as_of=AS_OF)["candidate_questions"]
    )
    assert questions == [
        "Beyond AWS Lambda, which AWS services have you used in production?",
        "Do you have hands-on PostgreSQL experience you can add to the CV?",
        "Which project shows design patterns, and can it go on the CV?",
        'Can you confirm this requirement: "Overlap with EU working hours (CET +/- 2 hours)."?',
    ]


def test_principal_role_adds_seniority_gap(bundle: ResumeBundle) -> None:
    evaluation = evaluate_resume_fit(bundle, "Experience with Storybook.", WEFLOW_ROLE, as_of=AS_OF)
    assert row(evaluation, "Principal-level scope")["evidence_status"] == "Unsupported"
    questions = cast("list[str]", evaluation["candidate_questions"])
    assert (
        "Which work shows Principal-level scope, such as technical decisions across several teams?"
        in questions
    )


def test_a_held_level_adds_no_seniority_gap(bundle: ResumeBundle) -> None:
    evaluation = evaluate_resume_fit(
        bundle, "Experience with Storybook.", "Senior Frontend Engineer", as_of=AS_OF
    )
    assert all(not str(item["requirement"]).endswith("-level scope") for item in matrix(evaluation))


def test_gap_rows_never_carry_a_claim(weflow: dict[str, object]) -> None:
    for item in matrix(weflow):
        if item["evidence_status"] in ("Adjacent", "Unsupported"):
            assert item["truthful_tailoring_action"] == (
                "Do not claim this. Ask the candidate (section 6)."
            )


# --- the Weflow regression ---

COMPANY_BLURB_FRAGMENTS = (
    "Revenue AI Platform automates",
    "200+ fast-growing companies",
    "backed by Gradient Ventures",
    "If you enjoy",
    "equal opportunity",
    "qualified applicants",
)


def test_weflow_has_no_company_context_rows(weflow: dict[str, object]) -> None:
    for item in matrix(weflow):
        text = str(item["requirement"])
        assert text not in ("Requirements", "Tasks", "Benefits")
        assert not any(fragment in text for fragment in COMPANY_BLURB_FRAGMENTS), text
    assert all(item["item_class"] != "company_context" for item in matrix(weflow))


def test_weflow_keeps_years_design_patterns_and_working_hours(
    weflow: dict[str, object],
) -> None:
    texts = [str(item["requirement"]) for item in matrix(weflow)]
    assert "5+ years of experience in software development." in texts
    assert any("design patterns" in text and "distributed computing" in text for text in texts)
    assert any("Overlap with EU working hours" in text for text in texts)


def test_weflow_aws_and_postgresql_are_not_direct(weflow: dict[str, object]) -> None:
    aws = row(weflow, "Experience with AWS services and cloud architecture.")
    postgres = row(weflow, "Expert-level proficiency in PostgreSQL")
    assert aws["evidence_status"] != "Direct"
    assert postgres["evidence_status"] != "Direct"


def test_weflow_report_has_gaps_and_at_least_three_questions(weflow: dict[str, object]) -> None:
    assert weflow["unsupported_or_partial_requirements"]
    assert len(cast("list[str]", weflow["candidate_questions"])) >= 3
    report = format_fit_evaluation_markdown(weflow, "Weflow", WEFLOW_ROLE)
    section_four = report.split("## 4.")[1].split("## 5.")[0]
    assert "- Unsupported: Expert-level proficiency in PostgreSQL" in section_four
    assert "None." not in report.split("## 6.")[1].split("## 7.")[0]


def test_weflow_benefits_are_listed_apart(weflow: dict[str, object]) -> None:
    benefits = cast("list[str]", weflow["benefits"])
    assert "Annual Paid Leave (PTO)." in benefits
    assert all("qualified applicants" not in benefit for benefit in benefits)
    assert all(str(item["requirement"]) not in benefits for item in matrix(weflow))
