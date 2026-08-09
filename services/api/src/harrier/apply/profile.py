"""The application profile: reusable stories, safe framing, and question
guidance, loaded from the profile store (spec 014 port of
application_profile.py).

Validation checks structure, never content; the profile body is the
operator's private data (ADR-008).
"""

from __future__ import annotations

import json
import sqlite3
from typing import cast

APPLICATION_PROFILE_KIND = "application_profile"
CANDIDATE_KIND = "candidate"

REQUIRED_MARKDOWN_HEADINGS = (
    "# Application Profile",
    "## Core Positioning",
    "## AI Tooling Workflow",
    "## Reusable Professional Stories",
    "## Engineering Principles",
    "## Safe Framing",
    "## Question Mapping",
    "## Style Guidance",
)
REQUIRED_JSON_KEYS = (
    "core_positioning",
    "ai_tooling_workflow",
    "professional_stories",
    "engineering_principles",
    "safe_framing",
    "question_mapping",
    "style_guidance",
)


class ApplicationProfileError(ValueError):
    pass


def _document(conn: sqlite3.Connection, kind: str, fmt: str) -> str | None:
    row = conn.execute(
        "SELECT content FROM profile_documents WHERE kind = ? AND format = ? ORDER BY name LIMIT 1",
        (kind, fmt),
    ).fetchone()
    return str(row[0]) if row is not None else None


def load_profile_markdown(conn: sqlite3.Connection) -> str:
    content = _document(conn, APPLICATION_PROFILE_KIND, "markdown")
    if content is None:
        raise ApplicationProfileError(
            "no application_profile markdown document in the profile store"
        )
    return content


def load_profile_json(conn: sqlite3.Connection) -> dict[str, object]:
    content = _document(conn, APPLICATION_PROFILE_KIND, "json")
    if content is None:
        raise ApplicationProfileError("no application_profile json document in the profile store")
    try:
        parsed: object = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ApplicationProfileError(f"application profile json is invalid: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ApplicationProfileError("application profile json is not an object")
    return cast("dict[str, object]", parsed)


def load_candidate_document(conn: sqlite3.Connection) -> dict[str, object]:
    content = _document(conn, CANDIDATE_KIND, "json")
    if content is None:
        raise ApplicationProfileError("no candidate document in the profile store")
    parsed: object = json.loads(content)
    if not isinstance(parsed, dict):
        raise ApplicationProfileError("candidate document is not an object")
    return cast("dict[str, object]", parsed)


def validate_profile_markdown(text: str) -> list[str]:
    return [
        f"missing markdown section: {heading}"
        for heading in REQUIRED_MARKDOWN_HEADINGS
        if heading not in text
    ]


def validate_profile_json(data: dict[str, object]) -> list[str]:
    errors = [f"missing json key: {key}" for key in REQUIRED_JSON_KEYS if key not in data]
    ai_raw = data.get("ai_tooling_workflow")
    ai = cast("dict[str, object]", ai_raw) if isinstance(ai_raw, dict) else {}
    if not ai.get("actual_tools"):
        errors.append("missing ai_tooling_workflow.actual_tools")
    if not ai.get("real_example"):
        errors.append("missing ai_tooling_workflow.real_example")
    stories = data.get("professional_stories")
    if not isinstance(stories, list) or not stories:
        errors.append("missing professional_stories")
    safe_raw = data.get("safe_framing")
    safe = cast("dict[str, object]", safe_raw) if isinstance(safe_raw, dict) else {}
    if not safe.get("do_not_claim"):
        errors.append("missing safe_framing.do_not_claim")
    if not data.get("question_mapping"):
        errors.append("missing question_mapping")
    return errors


def validate_profile(conn: sqlite3.Connection) -> list[str]:
    errors: list[str] = []
    try:
        errors.extend(validate_profile_markdown(load_profile_markdown(conn)))
    except ApplicationProfileError as exc:
        errors.append(str(exc))
    try:
        errors.extend(validate_profile_json(load_profile_json(conn)))
    except ApplicationProfileError as exc:
        errors.append(str(exc))
    return errors


def question_guidance(profile: dict[str, object], question: str) -> dict[str, object]:
    normalized = " ".join((question or "").strip().lower().split())
    mappings_raw = profile.get("question_mapping")
    mappings = cast("dict[str, object]", mappings_raw) if isinstance(mappings_raw, dict) else {}

    def as_guidance(value: object) -> dict[str, object]:
        if isinstance(value, dict):
            return cast("dict[str, object]", value)
        return {"guidance": value}

    for key, value in mappings.items():
        if key.lower() == normalized:
            return as_guidance(value)
    for key, value in mappings.items():
        key_norm = key.lower()
        if key_norm in normalized or normalized in key_norm:
            return as_guidance(value)
    if "ai" in normalized and "tool" in normalized:
        fallback = mappings.get("How do you use AI tools in your workflow?")
        if fallback is not None:
            return as_guidance(fallback)
    return {}


def build_question_guidance(
    profile: dict[str, object], questions: list[str]
) -> list[dict[str, object]]:
    return [
        {"question": question, "guidance": question_guidance(profile, question)}
        for question in questions
    ]
