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
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from harrier.apply.brief import (
    EMPTY_BRIEF,
    Brief,
    brief_instructions,
    limit_violations,
    never_name_hits,
    with_operator_evidence,
)
from harrier.apply.claims import (
    Claim,
    ClaimCheckError,
    ClaimContext,
    banned_hits,
    check_claims,
    parse_claims,
)
from harrier.apply.profile import (
    build_question_guidance,
    load_candidate_document,
    load_profile_json,
    load_profile_markdown,
    profile_text,
)
from harrier.apply.requirements import (
    Flag,
    is_opinion_question,
    posted_ranges,
    requirement_flags,
    requirement_kind,
)
from harrier.apply.review import Review, render_review
from harrier.db import data_dir
from harrier.llm import LLMClientError, generate_text
from harrier.llm.jsonparse import loads_tolerant
from harrier.resume.content import (
    forbidden_hits,
    load_forbidden_phrases,
    load_skill_vocabulary,
    load_truth_sources,
)

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
- Every fact about the candidate comes from resume_truth_source_md or latest_project_achievements_md.
- Use the application profile for style guidance and safe framing only. A profile story may be told only if the truth sources state it.
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

Claims and evidence, for each answer:
- Declare every factual sentence in the answer and its notes as a claim: the sentence exactly as written, whether it is about the candidate or the employer, and one or more evidence fragments.
- Evidence is quoted verbatim. Candidate evidence comes only from resume_truth_source_md or latest_project_achievements_md. Employer evidence comes only from job_description_text.
- Never cite job_description_text or the application profile as candidate evidence. A requirement the posting lists is something the employer wants, not something the candidate did. If a sentence about the candidate has no evidence in the truth sources, leave it out.
- Keep every number exactly as the evidence states it. A total over a period is never a rate, and a percentage keeps its sign.
- If the evidence describes a demo or synthetic data, say so in the sentence.
- Name a technology only if the truth sources show the candidate used it.
- If an answer needs a concrete example and none exists in the material, write [[TODO: what is needed]] instead of inventing one.
- Say "convention" or "warning" rather than "enforced" unless the material shows something enforcing it.
- Do not state model names, versions or other time-sensitive tool details unless the material supplies them.

Return strict JSON only with this shape:
{
  "answers": [
    {
      "question": "string",
      "short_answer": "string",
      "medium_answer": "string",
      "notes": ["string"],
      "claims": [
        {"sentence": "string", "about": "candidate or employer", "evidence": ["string"]}
      ]
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
    # What the answer rests on, listed under "To verify" (spec 066, B9).
    claims: list[Claim] = field(default_factory=list[Claim])


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
    """Whitespace only. Banned phrases used to be deleted here as substrings,
    which changed what an answer said without saying so; they now refuse the
    answer set instead (spec 065, rule N1)."""
    return " ".join(text.split()).strip()


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
    brief: Brief = EMPTY_BRIEF,
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
        # The application brief (spec 066). Empty values when there is none.
        "never_name": list(brief.never_name),
        "employer_guidance": brief.employer_guidance,
        "operator_evidence": list(brief.evidence),
        "operator_views": {
            question: view for question in questions if (view := brief.view_for(question))
        },
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
    payload_raw: object = loads_tolerant(extract_json_object(text))
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
                "claims": parse_claims(entry.get("claims")),
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
    brief: Brief = EMPTY_BRIEF,
) -> list[dict[str, object]]:
    payload = build_answers_payload(
        conn,
        company,
        role,
        questions,
        job_url=job_url,
        tracker_row=tracker_row,
        jd_text=jd_text,
        brief=brief,
    )
    prompt = (
        SYSTEM_PROMPT_BASE
        + style_guidance_prompt(load_profile_json(conn))
        + brief_instructions(brief, "answers")
    )
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


REQUIREMENT_PLACEHOLDER = "[[TODO: your answer]]"
OPINION_PLACEHOLDER = "[[TODO: your own view]]"
NUMBER_PLACEHOLDER = "[[TODO: your number]]"


def compensation_answer(posting: str, brief: Brief, candidate: dict[str, object]) -> str:
    """Assembled by code, never by the model (spec 066, B8): the posted
    ranges verbatim and the operator's own number, as a draft to edit."""
    ranges = posted_ranges(posting)
    posted = "; ".join(ranges) + "." if ranges else "none in the posting."
    number = brief.compensation_number
    if not number:
        compensation_raw = candidate.get("compensation")
        compensation = (
            cast("dict[str, object]", compensation_raw)
            if isinstance(compensation_raw, dict)
            else {}
        )
        target = compensation.get("salary_target_eur")
        if isinstance(target, int) and not isinstance(target, bool) and target > 0:
            number = f"EUR {target:,}"
    mine = f"{number}." if number else NUMBER_PLACEHOLDER
    return f"Draft for you to edit. This is not advice. Posted range: {posted} My number: {mine}"


def answer_without_the_model(
    question: str,
    brief: Brief,
    flags: list[Flag],
    posting: str,
    candidate: dict[str, object],
) -> AnswerDraft | None:
    """Questions no truth document can answer stay with the operator.

    Salary is assembled by code (B8). A hard requirement is a placeholder
    with the matching flags under it (B6). An opinion waits for the
    operator's view (B7). Anything else returns None and goes to the model.
    """
    if classify_question(question) == "salary":
        text = compensation_answer(posting, brief, candidate)
        return AnswerDraft(question, text, text, [])
    kind = requirement_kind(question)
    if kind is not None:
        notes = [f'Flag ({flag.kind}): "{flag.sentence}"' for flag in flags if flag.kind == kind]
        return AnswerDraft(question, REQUIREMENT_PLACEHOLDER, REQUIREMENT_PLACEHOLDER, notes)
    if is_opinion_question(question) and brief.view_for(question) is None:
        return AnswerDraft(question, OPINION_PLACEHOLDER, OPINION_PLACEHOLDER, [])
    return None


def generate_answer_set(
    conn: sqlite3.Connection,
    company: str,
    role: str,
    questions: list[str],
    job_url: str | None = None,
    tracker_row: dict[str, str] | None = None,
    jd_text: str | None = None,
    brief: Brief = EMPTY_BRIEF,
) -> list[AnswerDraft]:
    flags = requirement_flags(jd_text or "", brief.employer_guidance)
    candidate = load_candidate_document(conn)
    built: dict[int, AnswerDraft] = {}
    model_questions: list[str] = []
    for index, question in enumerate(questions):
        draft = answer_without_the_model(question, brief, flags, jd_text or "", candidate)
        if draft is None:
            model_questions.append(question)
        else:
            built[index] = draft
    if not model_questions:
        return [built[index] for index in range(len(questions))]

    generated = generate_ai_answers(
        conn,
        company,
        role,
        model_questions,
        job_url=job_url,
        tracker_row=tracker_row,
        jd_text=jd_text,
        brief=brief,
    )
    # Model answers are placed back by position among the questions sent, so
    # the count has to match or an answer would land under the wrong question.
    if len(generated) != len(model_questions):
        raise RuntimeError(
            f"AI returned {len(generated)} answers for {len(model_questions)} questions"
        )
    drafts = [
        AnswerDraft(
            question=str(item["question"]),
            short_answer=sanitize_answer_text(str(item["short_answer"])),
            medium_answer=sanitize_answer_text(str(item["medium_answer"])),
            notes=[
                sanitize_answer_text(str(note))
                for note in cast("list[object]", item.get("notes", []))
                if sanitize_answer_text(str(note))
            ],
            claims=list(cast("list[Claim]", item["claims"])),
        )
        for item in generated
    ]
    views = [view for question in model_questions if (view := brief.view_for(question))]
    context = ClaimContext(
        sources=with_operator_evidence(load_truth_sources(conn), *brief.evidence, *views),
        posting="\n".join(part for part in (jd_text or "", brief.employer_guidance) if part),
        company=company,
        role=role,
        vocabulary=load_skill_vocabulary(conn),
        profile=profile_text(conn),
    )
    forbidden_phrases = load_forbidden_phrases(conn)
    violations: list[str] = []
    for number, draft in enumerate(drafts, start=1):
        texts = [draft.short_answer, draft.medium_answer, *draft.notes]
        joined = "\n".join(texts)
        violations.extend(
            f"banned phrase: {phrase}" for phrase in banned_hits(BANNED_PHRASES, joined)
        )
        # The candidate's own never-claim list, over every field that is
        # written out. Spec 034 required it on the answers; nothing called it.
        violations.extend(
            f"forbidden phrase: {phrase}" for phrase in forbidden_hits(forbidden_phrases, joined)
        )
        violations.extend(
            f"named a redacted name: {name}" for name in never_name_hits(brief.never_name, joined)
        )
        for label, text in (("short", draft.short_answer), ("medium", draft.medium_answer)):
            violations.extend(
                limit_violations(text, brief.answers, "answers", f"answer {number} {label}")
            )
        violations.extend(check_claims(texts, draft.claims, context))
    if violations:
        raise ClaimCheckError(list(dict.fromkeys(violations)))

    model_drafts = iter(drafts)
    return [
        built[index] if index in built else next(model_drafts) for index in range(len(questions))
    ]


def render_markdown(
    company: str,
    role: str,
    job_url: str | None,
    tracker_row: dict[str, str] | None,
    drafts: list[AnswerDraft],
    review_path: Path | None = None,
    flags: Sequence[Flag] = (),
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
    markdown = "\n".join(lines).rstrip() + "\n"
    if review_path is None:
        return markdown
    texts = [text for d in drafts for text in (d.short_answer, d.medium_answer, *d.notes)]
    review = Review(
        claims=tuple(claim for draft in drafts for claim in draft.claims), flags=tuple(flags)
    )
    return markdown + "\n" + render_review(texts, review, review_path)


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
