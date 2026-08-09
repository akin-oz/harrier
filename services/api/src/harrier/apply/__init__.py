"""Cover letters and application answers (spec 014).

Recruiter-facing artifacts with style and PDF gates. Persona-free: the
prompts carry no candidate content; prose, stories, and guidance come
from the application profile documents in the profile store.
"""

from __future__ import annotations

from harrier.apply.answers import (
    DEFAULT_QUESTIONS,
    AnswerDraft,
    build_answers_payload,
    build_deterministic_draft,
    classify_question,
    generate_answer_set,
    parse_answers_response,
    parse_questions,
    render_markdown,
    sanitize_answer_text,
    write_output,
)
from harrier.apply.letters import (
    build_cover_letter_payload,
    generate_cover_letter,
    normalize_cover_letter_text,
    parse_cover_letter_response,
    render_cover_letter_html,
    render_cover_letter_markdown,
    validate_cover_letter,
    write_cover_letter_artifacts,
)
from harrier.apply.profile import (
    ApplicationProfileError,
    build_question_guidance,
    load_profile_json,
    load_profile_markdown,
    question_guidance,
    validate_profile,
)

__all__ = [
    "DEFAULT_QUESTIONS",
    "AnswerDraft",
    "ApplicationProfileError",
    "build_answers_payload",
    "build_cover_letter_payload",
    "build_deterministic_draft",
    "build_question_guidance",
    "classify_question",
    "generate_answer_set",
    "generate_cover_letter",
    "load_profile_json",
    "load_profile_markdown",
    "normalize_cover_letter_text",
    "parse_answers_response",
    "parse_cover_letter_response",
    "parse_questions",
    "question_guidance",
    "render_cover_letter_html",
    "render_cover_letter_markdown",
    "render_markdown",
    "sanitize_answer_text",
    "validate_cover_letter",
    "validate_profile",
    "write_cover_letter_artifacts",
    "write_output",
]
