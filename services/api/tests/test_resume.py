"""Behavior pins for tailored resume generation (spec 013), ported from the
old repo's tests/test_tailor_resume.py onto the synthetic persona in
config/resume-content.example.json (which these tests thereby prove valid).
"""

import copy
import json
from datetime import date
from pathlib import Path
from typing import cast

import pytest

from harrier.db import connect
from harrier.profile.store import put_document
from harrier.resume import (
    ResumeBundle,
    ResumeBundleError,
    TruthSources,
    apply_ai_bullet_order,
    build_content_plan,
    build_markdown,
    build_presentation_title,
    evaluate_resume_fit,
    normalize_visible_role_title,
    normalize_visible_url_text,
    parse_bundle,
    professional_experience_label,
    professional_experience_years,
    rank_skills,
    render_html,
    slugify,
    validate_content_plan,
)
from harrier.resume.markdown import resolve_bullets
from harrier.resume.ranking import rank_bullet_ids
from harrier.resume.tailor import run_tailor
from harrier.tracker import add_job, get_job

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_BUNDLE_PATH = REPO_ROOT / "config" / "resume-content.example.json"
AS_OF = date(2026, 8, 1)


def load_raw_bundle() -> dict[str, object]:
    return cast("dict[str, object]", json.loads(EXAMPLE_BUNDLE_PATH.read_text(encoding="utf-8")))


@pytest.fixture()
def bundle() -> ResumeBundle:
    return parse_bundle(load_raw_bundle())


@pytest.fixture()
def sources(bundle: ResumeBundle) -> TruthSources:
    return TruthSources(truth_text="\n".join(bundle.bullet_pool.values()), achievements_text="")


# ---------------------------------------------------------------------------
# Visible title and URL scrubbing
# ---------------------------------------------------------------------------


def test_normalize_visible_role_title_removes_internal_tailored_label() -> None:
    title = normalize_visible_role_title(
        "reedsy",
        "Tailored for reedsy — Senior Software Engineer (Node/Vue/TypeScript) - Remote Europe",
    )
    assert title == "Senior Software Engineer (Node/Vue/TypeScript) — Remote Europe"


def test_normalize_visible_role_title_removes_company_prefix_and_remote_suffix() -> None:
    assert (
        normalize_visible_role_title("peec", "peec — Senior Frontend Engineer (Remote)")
        == "Senior Frontend Engineer"
    )


def test_slug_still_keeps_company_for_output_filename() -> None:
    slug = slugify("reedsy-Senior Software Engineer (Node/Vue/TypeScript) - Remote Europe")
    assert slug.startswith("reedsy-")


def test_slugify_transliterates_turkish_characters() -> None:
    assert slugify("Deniz Örnek-Şirket") == "deniz-ornek-sirket"


def test_normalize_visible_url_text_strips_scheme_for_contact_line() -> None:
    assert (
        normalize_visible_url_text("https://linkedin.com/in/deniz-ornek")
        == "linkedin.com/in/deniz-ornek"
    )


# ---------------------------------------------------------------------------
# Grounded header and HTML
# ---------------------------------------------------------------------------

REQUESTED_ROLE = (
    "Tailored for reedsy — Senior Software Engineer (Node/Vue/TypeScript) - Remote Europe"
)


def test_markdown_header_uses_grounded_title_not_requested_identity(
    bundle: ResumeBundle, sources: TruthSources
) -> None:
    plan = build_content_plan(bundle, "", REQUESTED_ROLE, AS_OF)
    markdown = build_markdown(bundle, sources, plan)
    lines = [line for line in markdown.splitlines() if line.strip()]
    assert lines[0] == "# Deniz Örnek"
    assert lines[1] == "Senior Frontend Engineer — TypeScript & Vue 3"
    assert lines[2] == "Exampleland | deniz@example.com | linkedin.com/in/deniz-ornek"
    assert "Tailored for" not in markdown
    assert "reedsy — Senior Software Engineer" not in markdown


def test_html_header_uses_grounded_markdown_title(
    bundle: ResumeBundle, sources: TruthSources
) -> None:
    plan = build_content_plan(bundle, "", REQUESTED_ROLE, AS_OF)
    markdown = build_markdown(bundle, sources, plan)
    html = render_html(markdown, bundle, template_dir=REPO_ROOT / "templates")
    assert "Tailored for" not in html
    assert ">Senior Frontend Engineer — TypeScript &amp; Vue 3<" in html
    assert ">LinkedIn<" not in html
    assert ">linkedin.com/in/deniz-ornek<" in html
    assert 'href="https://linkedin.com/in/deniz-ornek"' in html


# ---------------------------------------------------------------------------
# Facts and identity gating
# ---------------------------------------------------------------------------


def test_experience_years_use_completed_anniversaries(bundle: ResumeBundle) -> None:
    assert professional_experience_years(bundle, date(2026, 5, 31)) == 9
    assert professional_experience_years(bundle, date(2026, 6, 1)) == 10
    assert professional_experience_label(bundle, date(2026, 7, 21)) == "10+ years"


def test_frontend_evidence_is_not_promoted_to_full_stack_from_jd(
    bundle: ResumeBundle,
) -> None:
    title = build_presentation_title(
        bundle,
        "Senior Frontend / Full-Stack Engineer",
        "React and TypeScript; some Node.js collaboration is useful.",
        AS_OF,
    )
    assert title.startswith("Senior Frontend Engineer")
    assert "Full-Stack" not in title


def test_genuine_full_stack_evidence_can_support_full_stack_title() -> None:
    raw = load_raw_bundle()
    roles = cast("list[dict[str, object]]", raw["roles"])
    cast("list[str]", roles[1]["competencies"]).append("backend")
    roles[2]["competencies"] = ["frontend", "backend"]
    full_stack_bundle = parse_bundle(raw)
    title = build_presentation_title(
        full_stack_bundle,
        "Senior Full-Stack Engineer",
        "React, TypeScript, and backend API development.",
        AS_OF,
    )
    assert title.startswith("Senior Full-Stack Engineer")


def test_react_target_prioritizes_react_and_typescript_over_vue(
    bundle: ResumeBundle,
) -> None:
    skills = rank_skills(bundle, "React TypeScript frontend product role", as_of=AS_OF)
    assert skills.index("React") < skills.index("Vue 3")
    assert skills.index("TypeScript") < skills.index("Nuxt")


def test_vue_target_prioritizes_vue_and_nuxt_over_react(bundle: ResumeBundle) -> None:
    skills = rank_skills(bundle, "Vue 3 and Nuxt frontend product role", as_of=AS_OF)
    assert skills.index("Vue 3") < skills.index("React")
    assert skills.index("Nuxt") < skills.index("React")


def test_ended_client_engagement_never_renders_as_present(bundle: ResumeBundle) -> None:
    plan = build_content_plan(bundle, "React TypeScript", "Senior Frontend Engineer", AS_OF)
    assert plan.role_periods["r1"] == "Nov 2025 – Jun 2026"  # noqa: RUF001
    assert "Present" not in plan.role_periods["r1"]
    assert validate_content_plan(plan, bundle) == []


# ---------------------------------------------------------------------------
# Evidence ranking and plan validation
# ---------------------------------------------------------------------------


def test_quantified_evidence_outranks_generic_jd_matching_bullet(
    bundle: ResumeBundle,
) -> None:
    ranked = rank_bullet_ids(
        bundle,
        ["ach_1", "r2_b7"],
        "Build scalable product applications and onboarding experiences.",
        "Senior Frontend Engineer",
    )
    assert ranked[0] == "ach_1"


def test_unsupported_jd_technology_cannot_enter_skills(bundle: ResumeBundle) -> None:
    plan = build_content_plan(bundle, "Kubernetes platform engineer", "Platform Engineer", AS_OF)
    assert "Kubernetes" not in plan.skills
    invalid = copy.deepcopy(plan)
    invalid.skills.insert(0, "Kubernetes")
    assert "skills lack candidate evidence: Kubernetes" in validate_content_plan(invalid, bundle)


def test_plan_rejects_duplicate_evidence_across_achievements_and_experience(
    bundle: ResumeBundle,
) -> None:
    plan = build_content_plan(bundle, "React TypeScript", "Senior Frontend Engineer", AS_OF)
    invalid = copy.deepcopy(plan)
    # Force the achievement's evidence group into a role selection too.
    invalid.role_bullets["r1"] = ["r1_b2", *invalid.role_bullets["r1"]]
    invalid.selected_achievements = ["ach_4", *invalid.selected_achievements]
    errors = validate_content_plan(invalid, bundle)
    assert any("duplicates the same evidence" in error for error in errors)


def test_ai_order_cannot_add_evidence(bundle: ResumeBundle) -> None:
    plan = build_content_plan(bundle, "React TypeScript", "Senior Frontend Engineer", AS_OF)
    reordered = apply_ai_bullet_order(
        plan, bundle, {"role1_bullets": ["r1_b9", "made_up"], "selected_achievements": []}
    )
    assert set(reordered.role_bullets["r1"]) == set(plan.role_bullets["r1"])
    assert validate_content_plan(reordered, bundle) == []


def test_bullet_failing_truth_check_is_omitted(bundle: ResumeBundle) -> None:
    partial_sources = TruthSources(truth_text=bundle.bullet_pool["r1_b1"], achievements_text="")
    texts, omitted = resolve_bullets(bundle, partial_sources, ["r1_b1", "r1_b2"])
    assert texts == [bundle.bullet_pool["r1_b1"]]
    assert omitted == ["r1_b2"]


def test_bundle_validation_names_unknown_refs() -> None:
    raw = load_raw_bundle()
    cast("dict[str, str]", raw["evidence_groups"])["ghost_bullet"] = "some_group"
    with pytest.raises(ResumeBundleError, match="ghost_bullet"):
        parse_bundle(raw)


# ---------------------------------------------------------------------------
# Fit evaluation
# ---------------------------------------------------------------------------


def test_fit_evaluation_marks_architecture_strong_and_backend_partial(
    bundle: ResumeBundle,
) -> None:
    jd = (
        "Own architecture and code boundaries for a scalable React and TypeScript "
        "product; backend API ownership is useful."
    )
    evaluation = evaluate_resume_fit(bundle, jd, "Senior Product Engineer")
    dimensions = {
        str(item["dimension"]): item
        for item in cast("list[dict[str, object]]", evaluation["dimensions"])
    }
    assert dimensions["architecture and code boundaries"]["evidence_status"] == "Strong evidence"
    assert dimensions["backend/full-stack ownership"]["evidence_status"] == "Partial evidence"
    assert "r1_b2" in cast(
        "list[str]", dimensions["architecture and code boundaries"]["evidence_refs"]
    )


def test_fit_evaluation_does_not_invent_game_or_ai_experience(
    bundle: ResumeBundle,
) -> None:
    jd = (
        "Build polished interfaces for a board-game product using React. "
        "An AI-first workflow is a plus."
    )
    evaluation = evaluate_resume_fit(bundle, jd, "Colonist Product Engineer")
    dimensions = {
        str(item["dimension"]): item
        for item in cast("list[dict[str, object]]", evaluation["dimensions"])
    }
    assert dimensions["domain motivation"]["evidence_status"] == "No evidence"
    assert dimensions["AI fluency"]["evidence_status"] == "No evidence"
    questions = cast("list[str]", evaluation["candidate_questions"])
    assert any("game/board-game" in question for question in questions)


def test_fit_evaluation_does_not_assume_salary_information(bundle: ResumeBundle) -> None:
    jd = "Senior frontend engineer. Salary range and relational database experience are required."
    evaluation = evaluate_resume_fit(bundle, jd, "Senior Frontend Engineer")
    questions = cast("list[str]", evaluation["candidate_questions"])
    assert any("compensation range" in question for question in questions)
    database = next(
        item
        for item in cast("list[dict[str, object]]", evaluation["dimensions"])
        if item["dimension"] == "databases and APIs"
    )
    assert database["evidence_status"] == "No evidence"


# ---------------------------------------------------------------------------
# End-to-end tailor run and the PDF gate
# ---------------------------------------------------------------------------


@pytest.fixture()
def tailor_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(REPO_ROOT)
    conn = connect()
    raw = EXAMPLE_BUNDLE_PATH.read_text(encoding="utf-8")
    put_document(conn, "resume_data", "resume-content.json", "json", raw)
    bundle_pool = cast("dict[str, str]", cast("dict[str, object]", json.loads(raw))["bullet_pool"])
    put_document(conn, "resume_truth", "truth.md", "markdown", "\n".join(bundle_pool.values()))
    job_id = add_job(
        conn,
        {
            "company": "Example Co",
            "title": "Senior Frontend Engineer",
            "location": "Remote, Europe",
            "url": "https://example.test/job",
            "source": "greenhouse",
            "status": "shortlisted",
        },
    )
    return job_id


def _fake_render(html_text: str, pdf_path: Path) -> None:
    pdf_path.write_bytes(b"%PDF-1.4 fake")


def test_failing_pdf_gate_leaves_tracker_row_unchanged(tailor_env: int) -> None:
    conn = connect()
    before = get_job(conn, tailor_env)

    def failing_validate(pdf_path: Path, html_text: str) -> list[str]:
        return ["rendered PDF has 2 pages; expected 1"]

    with pytest.raises(RuntimeError, match="render validation failed"):
        run_tailor(
            conn,
            tailor_env,
            jd_text="React and TypeScript product role.",
            no_ai=True,
            render=_fake_render,
            validate=failing_validate,
        )
    after = get_job(conn, tailor_env)
    assert after["status"] == before["status"] == "shortlisted"
    assert after["next_action"] == before["next_action"]


def test_passing_pdf_gate_updates_tracker_and_writes_artifacts(tailor_env: int) -> None:
    conn = connect()

    def passing_validate(pdf_path: Path, html_text: str) -> list[str]:
        return []

    result = run_tailor(
        conn,
        tailor_env,
        jd_text="React and TypeScript product role.",
        no_ai=True,
        render=_fake_render,
        validate=passing_validate,
    )
    assert result.pdf_path.exists()
    assert result.metadata_path.exists()
    assert result.evaluation_path is not None and result.evaluation_path.exists()
    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    assert metadata["ai_tailored"] is False
    assert "Tailored for" not in result.markdown_path.read_text(encoding="utf-8")
    assert get_job(conn, tailor_env)["status"] == "tailored_cv_requested"
