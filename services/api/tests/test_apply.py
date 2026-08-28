"""Behavior pins for cover letters and application answers (spec 014),
ported from the old repo's test_openai_cover_letters.py,
test_openai_answers.py, test_draft_application_answers.py, and
test_application_profile.py onto the synthetic profile in
config/application-profile.example.json and .md (which these tests
thereby prove valid)."""

import json
import sqlite3
from pathlib import Path
from typing import cast

import pytest

import harrier.apply.answers as answers_module
import harrier.apply.letters as letters_module
from harrier.apply import (
    DEFAULT_QUESTIONS,
    build_answers_payload,
    build_cover_letter_payload,
    build_deterministic_draft,
    generate_answer_set,
    generate_cover_letter,
    normalize_cover_letter_text,
    parse_answers_response,
    parse_cover_letter_response,
    parse_questions,
    question_guidance,
    render_cover_letter_html,
    render_markdown,
    validate_profile,
    write_cover_letter_artifacts,
)
from harrier.apply.answers import AnswerDraft
from harrier.db import connect
from harrier.llm import LLMClientError
from harrier.profile.store import put_document

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILE_JSON_PATH = REPO_ROOT / "config" / "application-profile.example.json"
PROFILE_MD_PATH = REPO_ROOT / "config" / "application-profile.example.md"

FULL_LETTER = (
    "I'm interested in Examplesoft because the work looks product-facing and close "
    "to the product.\n\n"
    "My strongest fit is TypeScript-first frontend and product engineering with concrete "
    "delivery in production.\n\n"
    "If that match holds in the process, I'd be glad to discuss the role further."
)
SHORT_LETTER = "I'm interested in this role because it fits my background well."


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    conn = connect()
    put_document(
        conn,
        "application_profile",
        "application-profile.json",
        "json",
        PROFILE_JSON_PATH.read_text(encoding="utf-8"),
    )
    put_document(
        conn,
        "application_profile",
        "application-profile.md",
        "markdown",
        PROFILE_MD_PATH.read_text(encoding="utf-8"),
    )
    put_document(
        conn,
        "candidate",
        "candidate.json",
        "json",
        json.dumps(
            {
                "candidate": {
                    "name": "Deniz Örnek",
                    "location": "Exampleland",
                    "email": "deniz@example.com",
                    "linkedin": "https://linkedin.com/in/deniz-ornek",
                },
                "compensation": {"salary_min_eur": 50000, "salary_target_eur": 60000},
            }
        ),
    )
    put_document(
        conn,
        "resume_truth",
        "truth.md",
        "markdown",
        "- Email: deniz@example.com\n- LinkedIn: https://linkedin.com/in/deniz-ornek\n",
    )
    return conn


def profile_json() -> dict[str, object]:
    return cast("dict[str, object]", json.loads(PROFILE_JSON_PATH.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# Application profile
# ---------------------------------------------------------------------------


def test_profile_validation_passes_on_committed_example(db: sqlite3.Connection) -> None:
    assert validate_profile(db) == []


def test_ai_tooling_question_guidance_resolves_story_ids() -> None:
    guidance = question_guidance(
        profile_json(),
        "Give us a specific example of how you use AI tools in your current workflow.",
    )
    story_ids = guidance.get("best_story_ids")
    assert isinstance(story_ids, list)
    assert "ai_assisted_workflow_automation" in cast("list[object]", story_ids)


def test_safe_framing_includes_do_not_claims() -> None:
    profile = profile_json()
    safe = cast("dict[str, object]", profile["safe_framing"])
    do_not_claim = cast("list[str]", safe["do_not_claim"])
    assert any("React Native" in item for item in do_not_claim)


# ---------------------------------------------------------------------------
# Answers: payload, parsing, sanitation, rendering
# ---------------------------------------------------------------------------


def test_build_answers_payload_includes_context(db: sqlite3.Connection) -> None:
    payload = build_answers_payload(
        db,
        "exampleco",
        "Senior Frontend Engineer (Node/Vue/TypeScript) - Remote Europe",
        ["Why are you interested in Exampleco and this role?"],
        job_url="https://jobs.ashbyhq.com/exampleco/123",
        tracker_row={"fit_score": "92", "status": "shortlisted", "notes": "strong fit"},
        jd_text="We need Vue, TypeScript, and product-minded engineering.",
    )
    assert payload["company"] == "exampleco"
    assert payload["job_url"] == "https://jobs.ashbyhq.com/exampleco/123"
    truth_sources = cast("dict[str, object]", payload["truth_sources"])
    assert "resume_truth_source_md" in truth_sources
    assert "application_profile_md" in truth_sources
    assert "application_profile_json" in truth_sources
    tracker_metadata = cast("dict[str, str]", payload["tracker_metadata"])
    assert tracker_metadata["fit_score"] == "92"
    assert payload["candidate_name"] == "Deniz Örnek"
    guidance_entries = cast(
        "list[dict[str, object]]", payload["application_profile_question_guidance"]
    )
    assert guidance_entries[0]["question"] == "Why are you interested in Exampleco and this role?"


def test_parse_answers_response_reads_json_payload() -> None:
    text = json.dumps(
        {
            "answers": [
                {
                    "question": "Why are you interested?",
                    "short_answer": "Because the role fits my background well.",
                    "medium_answer": "Because the product is useful and the work is close.",
                    "notes": ["Tie the answer to shipped frontend work."],
                }
            ]
        }
    )
    answers = parse_answers_response(text)
    assert len(answers) == 1
    assert answers[0]["notes"] == ["Tie the answer to shipped frontend work."]


def test_parse_answers_response_tolerates_trailing_comma() -> None:
    text = json.dumps(
        {
            "answers": [
                {
                    "question": "Why are you interested?",
                    "short_answer": "Because the role fits my background well.",
                    "medium_answer": "Because the product is useful and the work is close.",
                    "notes": ["Tie the answer to shipped frontend work."],
                }
            ]
        }
    )
    with_trailing = text[:-1] + ",\n}"
    answers = parse_answers_response(with_trailing)
    assert len(answers) == 1
    assert answers[0]["question"] == "Why are you interested?"


def test_generate_answers_propagates_ai_error(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(system_prompt: str, user_input: str) -> str:
        raise LLMClientError("configured backend is unavailable")

    monkeypatch.setattr(answers_module, "generate_text", boom)
    with pytest.raises(RuntimeError, match="AI request failed"):
        generate_answer_set(db, "exampleco", "Senior Software Engineer", ["Why?"])


def test_generated_answers_avoid_banned_phrases(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_generate(system_prompt: str, user_input: str) -> str:
        return json.dumps(
            {
                "answers": [
                    {
                        "question": question,
                        "short_answer": "I am thrilled to apply.",
                        "medium_answer": (
                            "I am passionate about this amazing opportunity "
                            "in a dynamic environment."
                        ),
                        "notes": ["cutting-edge", "world-class"],
                    }
                    for question in DEFAULT_QUESTIONS
                ]
            }
        )

    monkeypatch.setattr(answers_module, "generate_text", fake_generate)
    drafts = generate_answer_set(db, "exampleco", "Senior Software Engineer", DEFAULT_QUESTIONS)
    joined = "\n".join(draft.short_answer + "\n" + draft.medium_answer for draft in drafts).lower()
    for phrase in ("i am thrilled", "i am passionate about", "amazing opportunity", "cutting-edge"):
        assert phrase not in joined


def test_parse_questions_file_mode_strips_bullets_and_numbers(tmp_path: Path) -> None:
    questions_path = tmp_path / "questions.txt"
    questions_path.write_text(
        "- Why are you a fit for this role?\n2. What relevant experience do you have?\n",
        encoding="utf-8",
    )
    assert parse_questions(None, str(questions_path)) == [
        "Why are you a fit for this role?",
        "What relevant experience do you have?",
    ]


def test_render_markdown_has_draft_sections() -> None:
    content = render_markdown(
        "exampleco",
        "Senior Software Engineer",
        "https://example.test/job",
        {"fit_score": "92", "status": "shortlisted"},
        [
            AnswerDraft(
                question="Why are you interested?",
                short_answer="Short.",
                medium_answer="Medium.",
                notes=["A note."],
            )
        ],
    )
    assert "### Short Draft Answer" in content
    assert "### Medium Draft Answer" in content
    assert "- Tracker score: 92" in content


def test_deterministic_salary_answer_uses_candidate_compensation(
    db: sqlite3.Connection,
) -> None:
    from harrier.apply.profile import load_candidate_document, load_profile_json

    draft = build_deterministic_draft(
        "What are your salary expectations?",
        "exampleco",
        load_profile_json(db),
        load_candidate_document(db),
    )
    assert "60,000" in draft.short_answer
    assert "50,000" in draft.medium_answer


def test_deterministic_interest_answer_fills_company_and_product_signal(
    db: sqlite3.Connection,
) -> None:
    from harrier.apply.profile import load_candidate_document, load_profile_json

    draft = build_deterministic_draft(
        "Why are you interested in this company and this role?",
        "exampleco",
        load_profile_json(db),
        load_candidate_document(db),
        jd_text="A synthetic product used only to exercise the JD signal path.",
    )
    assert draft.medium_answer.startswith("I'm interested in Exampleco because")
    assert "the product is useful and the users are real" in draft.medium_answer


# ---------------------------------------------------------------------------
# Cover letters
# ---------------------------------------------------------------------------


def test_build_cover_letter_payload_includes_context(db: sqlite3.Connection) -> None:
    payload = build_cover_letter_payload(
        db,
        "examplesoft",
        "Senior Product Engineer (Remote)",
        job_url="https://jobs.ashbyhq.com/examplesoft/123",
        tracker_row={"fit_score": "80", "status": "shortlisted", "notes": "strong fit"},
        jd_text="Product-facing engineering with TypeScript.",
        extra_notes="Keep it compact and recruiter-facing.",
    )
    assert payload["company"] == "examplesoft"
    assert payload["extra_notes"] == "Keep it compact and recruiter-facing."
    tracker_metadata = cast("dict[str, str]", payload["tracker_metadata"])
    assert tracker_metadata["fit_score"] == "80"
    truth_sources = cast("dict[str, object]", payload["truth_sources"])
    assert "application_profile_json" in truth_sources


def test_parse_cover_letter_response_reads_json_payload() -> None:
    text = json.dumps({"short_version": SHORT_LETTER, "full_version": FULL_LETTER})
    parsed = parse_cover_letter_response(text)
    assert "Examplesoft" in parsed["full_version"]


def test_parse_cover_letter_response_tolerates_trailing_comma() -> None:
    # The observed failure shape (spec 058): both fields valid, one
    # trailing comma before the closing brace.
    text = json.dumps({"short_version": SHORT_LETTER, "full_version": FULL_LETTER})
    with_trailing = text[:-1] + ",\n}"
    parsed = parse_cover_letter_response(with_trailing)
    assert parsed["short_version"] == SHORT_LETTER
    assert parsed["full_version"] == FULL_LETTER


def test_normalize_cover_letter_text_removes_internal_dump_language() -> None:
    text = (
        "Fit:\n- Tailored for Examplesoft\n- I can send those on request.\n\n"
        "Most relevant to this role: I am thrilled about this amazing opportunity.\n\n"
        "Practically, I would be honored."
    )
    normalized = normalize_cover_letter_text(text, is_full=True)
    for phrase in (
        "Fit:",
        "Tailored for",
        "I can send those on request",
        "I am thrilled",
        "Most relevant to this role",
        "Practically",
    ):
        assert phrase not in normalized


def test_write_cover_letter_artifacts_creates_md_html_pdf(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    def fake_render(html_text: str, pdf_path: Path) -> None:
        pdf_path.write_bytes(b"%PDF-1.4\n")

    def passing_validate(pdf_path: Path, html_text: str) -> list[str]:
        return []

    artifacts = write_cover_letter_artifacts(
        db,
        "examplesoft",
        "Senior Product Engineer (Remote)",
        "https://jobs.ashbyhq.com/examplesoft/123",
        SHORT_LETTER,
        FULL_LETTER,
        output_dir=tmp_path / "letters",
        template_dir=REPO_ROOT / "templates",
        render=fake_render,
        validate=passing_validate,
    )
    assert artifacts["markdown"].exists()
    assert artifacts["html"].exists()
    assert artifacts["pdf"].exists()
    markdown = artifacts["markdown"].read_text(encoding="utf-8")
    html = artifacts["html"].read_text(encoding="utf-8")
    assert "## Short Version" in markdown
    assert "## Full Version" in markdown
    assert "Short Version" not in html
    assert "Cover Letter" not in html


def test_write_cover_letter_artifacts_fails_when_pdf_not_created(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    def no_render(html_text: str, pdf_path: Path) -> None:
        return None

    with pytest.raises(RuntimeError, match="PDF was not created or is empty"):
        write_cover_letter_artifacts(
            db,
            "examplesoft",
            "Senior Product Engineer (Remote)",
            "https://jobs.ashbyhq.com/examplesoft/123",
            SHORT_LETTER,
            FULL_LETTER,
            output_dir=tmp_path / "letters",
            template_dir=REPO_ROOT / "templates",
            render=no_render,
        )


def test_render_cover_letter_html_contains_only_full_letter(db: sqlite3.Connection) -> None:
    html = render_cover_letter_html(
        db,
        "examplesoft",
        "Senior Product Engineer (Remote)",
        FULL_LETTER,
        template_dir=REPO_ROOT / "templates",
    )
    assert "Short Version" not in html
    assert "Cover Letter" not in html
    assert "examplesoft — senior product engineer" not in html.lower()
    assert "linkedin.com/in/deniz-ornek" in html


def test_generate_cover_letter_propagates_ai_error(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(system_prompt: str, user_input: str) -> str:
        raise LLMClientError("configured backend is unavailable")

    monkeypatch.setattr(letters_module, "generate_text", boom)
    with pytest.raises(RuntimeError, match="AI request failed"):
        generate_cover_letter(db, "exampleco", "Senior Software Engineer")


def test_generate_cover_letter_validates_three_paragraphs(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def two_paragraph_response(system_prompt: str, user_input: str) -> str:
        return json.dumps(
            {
                "short_version": SHORT_LETTER,
                "full_version": (
                    "I'm interested in Examplesoft because the work looks product-facing today.\n\n"
                    "My strongest fit is TypeScript-first frontend engineering in production."
                ),
            }
        )

    monkeypatch.setattr(letters_module, "generate_text", two_paragraph_response)
    with pytest.raises(ValueError, match="three short paragraphs"):
        generate_cover_letter(db, "examplesoft", "Senior Product Engineer")
