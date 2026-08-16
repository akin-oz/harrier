"""Application answer drafts (spec 014 port of application_answers_lib.py
and openai_answers.py).

Two paths: the LLM path through harrier.llm, and a deterministic path
whose prose templates live in the application profile json (stated
change: no candidate prose in code). Both sanitize banned phrasing.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from harrier.apply.profile import (
    build_question_guidance,
    load_candidate_document,
    load_profile_json,
    load_profile_markdown,
)
from harrier.db import data_dir
from harrier.llm import LLMClientError, generate_text
from harrier.resume.content import load_truth_sources

logger = logging.getLogger(__name__)

DEFAULT_QUESTIONS = [
    "Why are you interested in this company and this role?",
    "Why are you a fit for this role?",
    "What relevant experience do you have?",
    "Why are you leaving your current role?",
    "What are your salary expectations?",
    "What is your notice period or availability?",
]

BANNED_PHRASES = [
    "i am thrilled",
    "i am passionate about",
    "dream role",
    "amazing opportunity",
    "cutting-edge",
    "world-class",
    "dynamic environment",
    "innovative solutions",
    "leverage",
    "spearheaded",
    "drove transformation",
    "excited to join",
    "fast-paced environment",
    "i strongly believe i am the perfect candidate",
    "i would be honored",
    "your amazing team",
    "this incredible opportunity",
]

SYSTEM_PROMPT_BASE = (
    """You generate recruiter-facing draft answers for the candidate's job application questions.

Write like a thoughtful senior engineer writing quickly but carefully.

Core voice:
- direct
- practical
- low-fluff
- slightly compressed
- grounded
- understated
- evidence-first
- recruiter-facing, not theatrical

Non-negotiable rules:
- Truthful only.
- Do not invent experience, tools, domains, or responsibilities.
- Use only the supplied truth sources and metadata.
- Use the application profile for reusable stories, AI-tooling context, style guidance, and safe framing.
- Prefer safe framing over overstating adjacent experience.
- No corporate filler.
- No cover-letter template tone.
- No "Tailored for ..." wording.
- No cliches or inflated enthusiasm.
- Keep answers concise.
- Short answers: 1 to 3 sentences.
- Medium answers: 3 to 6 sentences.
- Prefer one idea per sentence.

Banned phrasing:
"""
    + "\n".join(f"- {phrase}" for phrase in BANNED_PHRASES)
    + """

Return strict JSON only with this shape:
{
  "answers": [
    {
      "question": "string",
      "short_answer": "string",
      "medium_answer": "string",
      "notes": ["string"]
    }
  ]
}

FORMATTING: Never use em dashes anywhere in the output. Use commas, semicolons, colons, or hyphens instead.
"""
)


@dataclass
class AnswerDraft:
    question: str
    short_answer: str
    medium_answer: str
    notes: list[str]


def answers_dir() -> Path:
    return data_dir() / "answers"


def slugify(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return re.sub(r"-{2,}", "-", value)


def normalize(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def display_company_name(company: str) -> str:
    value = (company or "").strip()
    if not value:
        return value
    return value[0].upper() + value[1:] if value == value.lower() else value


def parse_questions(question: str | None, questions_file: str | None) -> list[str]:
    if question and question.strip():
        return [question.strip()]
    if questions_file:
        questions: list[str] = []
        for raw_line in Path(questions_file).read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            line = re.sub(r"^[-*]\s+", "", line)
            line = re.sub(r"^\d+\.\s+", "", line)
            if line:
                questions.append(line)
        return questions
    return DEFAULT_QUESTIONS[:]


def classify_question(question: str) -> str:
    q = normalize(question)
    if "salary" in q or "compensation" in q or "salary expectation" in q:
        return "salary"
    if "notice period" in q or "availability" in q or "start date" in q:
        return "availability"
    if "leaving" in q or "why do you want to leave" in q or "why are you leaving" in q:
        return "leaving"
    if "fit" in q or "why should we hire" in q:
        return "fit"
    if "relevant experience" in q or "what experience" in q or "background" in q:
        return "experience"
    if "interested" in q or "work here" in q or "why this company" in q or "why this role" in q:
        return "interest"
    return "generic"


def jd_product_signal(jd_text: str | None, company: str) -> str | None:
    if not jd_text:
        return None
    text = normalize(jd_text)
    if any(
        keyword in text
        for keyword in ["author", "reader", "creator", "customer", "user", "marketplace", "editor"]
    ):
        return "the product is useful and the users are real"
    if any(keyword in text for keyword in ["workflow", "platform", "tooling", "productivity"]):
        return "the product problems look practical and user-facing"
    if normalize(company) in text:
        return "the product itself looks close to the role, not separate from it"
    return None


def sanitize_answer_text(text: str) -> str:
    value = " ".join(text.split())
    for banned in BANNED_PHRASES:
        if banned in value.lower():
            value = re.sub(re.escape(banned), "", value, flags=re.IGNORECASE)
            value = " ".join(value.split())
    return value.strip()


# ---------------------------------------------------------------------------
# Deterministic path: templates from the application profile json
# ---------------------------------------------------------------------------


def _template_pair(templates: dict[str, object], kind: str) -> tuple[str, str, list[str]] | None:
    entry_raw = templates.get(kind)
    if not isinstance(entry_raw, dict):
        return None
    entry = cast("dict[str, object]", entry_raw)
    short = entry.get("short")
    medium = entry.get("medium")
    notes_raw = entry.get("notes")
    notes = (
        [str(note) for note in cast("list[object]", notes_raw)]
        if isinstance(notes_raw, list)
        else []
    )
    if isinstance(short, str) and isinstance(medium, str):
        return short, medium, notes
    return None


def build_deterministic_draft(
    question: str,
    company: str,
    profile: dict[str, object],
    candidate: dict[str, object],
    jd_text: str | None = None,
) -> AnswerDraft:
    """Fill the profile's deterministic template for the question kind.

    The classifier and product-signal heuristic are code; every sentence
    of prose comes from the profile (stated change from the old builders).
    """
    kind = classify_question(question)
    templates_raw = profile.get("deterministic_answers")
    templates = cast("dict[str, object]", templates_raw) if isinstance(templates_raw, dict) else {}
    pair = _template_pair(templates, kind) or _template_pair(templates, "generic")
    if pair is None:
        raise ValueError(
            f"application profile has no deterministic_answers template for {kind!r} or 'generic'"
        )
    short, medium, notes = pair

    compensation_raw = candidate.get("compensation")
    compensation = (
        cast("dict[str, object]", compensation_raw) if isinstance(compensation_raw, dict) else {}
    )
    salary_min = compensation.get("salary_min_eur", 0)
    salary_target = compensation.get("salary_target_eur", 0)
    values = {
        "company": display_company_name(company),
        "product_signal": jd_product_signal(jd_text, company)
        or "the product is useful and the users are real",
        "salary_min": f"{salary_min:,}" if isinstance(salary_min, int) else str(salary_min),
        "salary_target": f"{salary_target:,}"
        if isinstance(salary_target, int)
        else str(salary_target),
    }

    def fill(template: str) -> str:
        result = template
        for key, value in values.items():
            result = result.replace("{" + key + "}", value)
        return result

    return AnswerDraft(
        question=question,
        short_answer=sanitize_answer_text(fill(short)),
        medium_answer=sanitize_answer_text(fill(medium)),
        notes=[sanitize_answer_text(fill(note)) for note in notes if note.strip()],
    )


# ---------------------------------------------------------------------------
# LLM path
# ---------------------------------------------------------------------------


def _tracker_metadata(tracker_row: dict[str, str] | None) -> dict[str, str] | None:
    if not tracker_row:
        return None
    return {
        "company": tracker_row.get("company", ""),
        "title": tracker_row.get("title", ""),
        "location": tracker_row.get("location", ""),
        "url": tracker_row.get("url", ""),
        "source": tracker_row.get("source", ""),
        "fit_score": tracker_row.get("fit_score", ""),
        "status": tracker_row.get("status", ""),
        "next_action": tracker_row.get("next_action", ""),
        "notes": tracker_row.get("notes", ""),
    }


def build_answers_payload(
    conn: sqlite3.Connection,
    company: str,
    role: str,
    questions: list[str],
    job_url: str | None = None,
    tracker_row: dict[str, str] | None = None,
    jd_text: str | None = None,
) -> dict[str, object]:
    profile = load_profile_json(conn)
    candidate = load_candidate_document(conn)
    sources = load_truth_sources(conn)
    candidate_block_raw = candidate.get("candidate")
    candidate_block = (
        cast("dict[str, object]", candidate_block_raw)
        if isinstance(candidate_block_raw, dict)
        else {}
    )
    return {
        "candidate_name": str(candidate_block.get("name", "")),
        "company": company,
        "role": role,
        "job_url": job_url or "",
        "tracker_metadata": _tracker_metadata(tracker_row),
        "questions": questions,
        "job_description_text": jd_text or "",
        "application_profile_question_guidance": build_question_guidance(profile, questions),
        "truth_sources": {
            "resume_truth_source_md": sources.truth_text,
            "latest_project_achievements_md": sources.achievements_text,
            "candidate_json": candidate,
            "application_profile_md": load_profile_markdown(conn),
            "application_profile_json": profile,
        },
    }


def style_guidance_prompt(profile: dict[str, object]) -> str:
    """Candidate-specific guidance appended to the base prompt as data."""
    guidance = profile.get("style_guidance")
    if not guidance:
        return ""
    return "\nStyle guidance from the application profile:\n" + json.dumps(
        guidance, ensure_ascii=False, indent=2
    )


def extract_json_object(text: str) -> str:
    value = (text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
        value = value.strip()
    start = value.find("{")
    end = value.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("AI response did not contain JSON")
    return value[start : end + 1]


def parse_answers_response(text: str) -> list[dict[str, object]]:
    payload_raw: object = json.loads(extract_json_object(text))
    payload = cast("dict[str, object]", payload_raw) if isinstance(payload_raw, dict) else {}
    answers = payload.get("answers")
    if not isinstance(answers, list) or not answers:
        raise ValueError("AI response did not contain answers")
    normalized: list[dict[str, object]] = []
    for item in cast("list[object]", answers):
        if not isinstance(item, dict):
            continue
        entry = cast("dict[str, object]", item)
        question = str(entry.get("question", "")).strip()
        short_answer = str(entry.get("short_answer", "")).strip()
        medium_answer = str(entry.get("medium_answer", "")).strip()
        notes_raw = entry.get("notes")
        notes = (
            [str(note).strip() for note in cast("list[object]", notes_raw) if str(note).strip()]
            if isinstance(notes_raw, list)
            else []
        )
        if not question or not short_answer or not medium_answer:
            raise ValueError("AI response was missing required answer fields")
        normalized.append(
            {
                "question": question,
                "short_answer": short_answer,
                "medium_answer": medium_answer,
                "notes": notes,
            }
        )
    if not normalized:
        raise ValueError("AI response did not contain usable answers")
    return normalized


def generate_ai_answers(
    conn: sqlite3.Connection,
    company: str,
    role: str,
    questions: list[str],
    job_url: str | None = None,
    tracker_row: dict[str, str] | None = None,
    jd_text: str | None = None,
) -> list[dict[str, object]]:
    payload = build_answers_payload(
        conn, company, role, questions, job_url=job_url, tracker_row=tracker_row, jd_text=jd_text
    )
    prompt = SYSTEM_PROMPT_BASE + style_guidance_prompt(load_profile_json(conn))
    try:
        output_text = generate_text(prompt, json.dumps(payload, ensure_ascii=False, indent=2))
    except LLMClientError as exc:
        raise RuntimeError(f"AI request failed: {exc}") from exc
    if not output_text.strip():
        raise RuntimeError("AI backend returned an empty response")
    try:
        return parse_answers_response(output_text)
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"failed to parse AI response: {exc}") from exc


def generate_answer_set(
    conn: sqlite3.Connection,
    company: str,
    role: str,
    questions: list[str],
    job_url: str | None = None,
    tracker_row: dict[str, str] | None = None,
    jd_text: str | None = None,
) -> list[AnswerDraft]:
    generated = generate_ai_answers(
        conn, company, role, questions, job_url=job_url, tracker_row=tracker_row, jd_text=jd_text
    )
    drafts = [
        AnswerDraft(
            question=str(item["question"]),
            short_answer=str(item["short_answer"]),
            medium_answer=str(item["medium_answer"]),
            notes=[str(note) for note in cast("list[object]", item.get("notes", []))],
        )
        for item in generated
    ]
    for draft in drafts:
        draft.short_answer = sanitize_answer_text(draft.short_answer)
        draft.medium_answer = sanitize_answer_text(draft.medium_answer)
        draft.notes = [
            sanitize_answer_text(note) for note in draft.notes if sanitize_answer_text(note)
        ]
    return drafts


def render_markdown(
    company: str,
    role: str,
    job_url: str | None,
    tracker_row: dict[str, str] | None,
    drafts: list[AnswerDraft],
) -> str:
    lines = [
        "# Application Answer Drafts",
        "",
        f"- Company: {display_company_name(company)}",
        f"- Role: {role}",
    ]
    if job_url:
        lines.append(f"- Job URL: {job_url}")
    if tracker_row:
        lines.append(f"- Tracker score: {tracker_row.get('fit_score', '')}")
        lines.append(f"- Tracker status: {tracker_row.get('status', '')}")
    lines.append("")
    for index, draft in enumerate(drafts, start=1):
        lines.extend(
            [
                f"## Question {index}",
                draft.question,
                "",
                "### Short Draft Answer",
                draft.short_answer,
                "",
                "### Medium Draft Answer",
                draft.medium_answer,
                "",
            ]
        )
        if draft.notes:
            lines.append("### Notes")
            lines.extend(f"- {note}" for note in draft.notes)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def answers_path_for(company: str, role: str, output_dir: Path | None = None) -> Path:
    """Where an answers run puts its file.

    Shared by the writer below and by the reader that serves it back, so the
    two cannot disagree about where it is (spec 047).
    """
    directory = output_dir if output_dir is not None else answers_dir()
    return directory / f"{slugify(f'{company}-{role}')}.md"


def write_output(company: str, role: str, content: str, output_dir: Path | None = None) -> Path:
    directory = output_dir if output_dir is not None else answers_dir()
    directory.mkdir(parents=True, exist_ok=True)
    output_path = answers_path_for(company, role, directory)
    output_path.write_text(content, encoding="utf-8")
    return output_path
