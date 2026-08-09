"""Cover letters (spec 014 port of openai_cover_letters.py).

Three short paragraphs, banned phrasing stripped and validated, PDF or
failure. The HTML and PDF contain only the full letter: no internal
section labels ever reach the recruiter-facing artifact.
"""

from __future__ import annotations

import html
import json
import logging
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import cast

from harrier.apply.answers import extract_json_object, slugify
from harrier.apply.profile import (
    load_candidate_document,
    load_profile_json,
    load_profile_markdown,
)
from harrier.db import data_dir
from harrier.llm import LLMClientError, generate_text
from harrier.resume.content import load_truth_sources
from harrier.resume.markdown import normalize_visible_url_text
from harrier.resume.pdf import render_pdf

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path("templates")
HTML_TEMPLATE = "cover-letter-template.html"
CSS_TEMPLATE = "cover-letter-template.css"

BANNED_PHRASES = [
    "fit:",
    "tailored for",
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
    "i would be honored",
    "i can send those on request",
    "most relevant to this role",
    "practically",
    "that aligns with the kind of",
    "add immediate value",
    "short technical conversation",
    "current priorities",
    "i bring",
    "i offer",
    "i come with",
]

SYSTEM_PROMPT_BASE = (
    """You generate recruiter-facing cover letters for the candidate.

Write like a thoughtful senior engineer writing quickly but carefully.

Core voice:
- direct
- practical
- low-fluff
- understated
- evidence-first
- slightly compressed
- recruiter-facing, not theatrical
- human, not polished marketing copy

You are writing a real cover letter, not an internal qualification summary.
The reader is usually a recruiter or hiring manager skimming quickly.

Required structure for full_version:
1. Short opening paragraph: why this company and this role specifically.
2. Short middle paragraph: strongest relevant fit with 2 to 3 concrete points.
3. Short closing paragraph: practical interest and next step.

Hard constraints:
- No "Fit:"
- No bullet-list voice
- No internal or debug wording
- No "I can send those on request"
- No fake enthusiasm
- No flattering praise
- No corporate filler
- No invented experience
- Use the application profile for positioning, story selection, and safe framing.
- Usually 170 to 240 words max unless the user explicitly asks for longer
- Prefer 3 short paragraphs of 2 to 3 sentences each
- Pick 1 or 2 proof points, not a full career summary
- Do not stack long lists of tools, processes, or metrics in one sentence
- Do not sound like a generated qualification brief
- Do not restate the entire resume
- Do not include relocation, visa, or geography logistics unless the user or supplied notes explicitly make that relevant
- Do not say "Most relevant to this role", "Practically", "That aligns with", or similar scaffolding
- If role context is strong, be specific; if not, stay simple

Short version:
- suitable for an application textbox or intro email
- 2 to 4 sentences
- direct, not salesy

Full version:
- 3 short paragraphs
- plain text paragraphs separated by blank lines
- mention the company and role in the opening
- make the company-specific tailoring concrete if the product or role context supports it
- if context is weak, stay honest and simple rather than generic
- do not force a company compliment
- do not mention more than 2 named tools or systems in the whole letter unless the role clearly requires it
- at most 1 sentence with numbers/metrics

Banned phrasing:
"""
    + "\n".join(f"- {phrase}" for phrase in BANNED_PHRASES)
    + """

Return strict JSON only with this shape:
{
  "short_version": "string",
  "full_version": "string"
}

FORMATTING: Never use em dashes anywhere in the output. Use commas, semicolons, colons, or hyphens instead.
"""
)


def cover_letters_dir() -> Path:
    return data_dir() / "cover-letters"


def candidate_contact(conn: sqlite3.Connection) -> dict[str, str]:
    candidate = load_candidate_document(conn)
    block_raw = candidate.get("candidate")
    block = cast("dict[str, object]", block_raw) if isinstance(block_raw, dict) else {}
    sources = load_truth_sources(conn)
    email_match = re.search(r"- Email: (.+)", sources.truth_text)
    linkedin_match = re.search(r"- LinkedIn: (.+)", sources.truth_text)
    linkedin_url = str(block.get("linkedin") or "") or (
        linkedin_match.group(1).strip() if linkedin_match else ""
    )
    return {
        "name": str(block.get("name", "")),
        "location": str(block.get("location", "")),
        "email": str(block.get("email") or "")
        or (email_match.group(1).strip() if email_match else ""),
        "linkedin_url": linkedin_url,
        "linkedin_label": normalize_visible_url_text(linkedin_url),
    }


def build_cover_letter_payload(
    conn: sqlite3.Connection,
    company: str,
    role: str,
    job_url: str | None = None,
    tracker_row: dict[str, str] | None = None,
    jd_text: str | None = None,
    extra_notes: str | None = None,
) -> dict[str, object]:
    tracker_metadata = None
    if tracker_row:
        tracker_metadata = {
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
    candidate = load_candidate_document(conn)
    block_raw = candidate.get("candidate")
    block = cast("dict[str, object]", block_raw) if isinstance(block_raw, dict) else {}
    sources = load_truth_sources(conn)
    return {
        "candidate_name": str(block.get("name", "")),
        "company": company,
        "role": role,
        "job_url": job_url or "",
        "tracker_metadata": tracker_metadata,
        "job_description_text": jd_text or "",
        "extra_notes": extra_notes or "",
        "truth_sources": {
            "resume_truth_source_md": sources.truth_text,
            "latest_project_achievements_md": sources.achievements_text,
            "candidate_json": candidate,
            "application_profile_md": load_profile_markdown(conn),
            "application_profile_json": load_profile_json(conn),
        },
    }


def parse_cover_letter_response(text: str) -> dict[str, str]:
    payload_raw: object = json.loads(extract_json_object(text))
    payload = cast("dict[str, object]", payload_raw) if isinstance(payload_raw, dict) else {}
    short_version = str(payload.get("short_version", "")).strip()
    full_version = str(payload.get("full_version", "")).strip()
    if not short_version or not full_version:
        raise ValueError("AI response was missing cover letter fields")
    return {"short_version": short_version, "full_version": full_version}


def strip_banned_phrases(text: str) -> str:
    value = text
    for banned in BANNED_PHRASES:
        value = re.sub(re.escape(banned), "", value, flags=re.IGNORECASE)
    return value


def trim_to_word_limit(text: str, max_words: int = 300) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text.strip()
    trimmed = " ".join(words[:max_words]).strip()
    sentence_end = max(trimmed.rfind("."), trimmed.rfind("!"), trimmed.rfind("?"))
    if sentence_end > max_words // 4:
        trimmed = trimmed[: sentence_end + 1]
    return trimmed.strip()


def normalize_cover_letter_text(text: str, *, is_full: bool) -> str:
    value = (text or "").replace("\r\n", "\n")
    value = strip_banned_phrases(value)
    value = re.sub(r"^[\-\*•]\s+", "", value, flags=re.MULTILINE)
    value = re.sub(r"^\s*paragraph\s*\d+\s*:\s*", "", value, flags=re.IGNORECASE | re.MULTILINE)
    value = re.sub(
        r"^\s*(most relevant to this role|practically)\s*:?\s*",
        "",
        value,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if is_full:
        blocks = [
            re.sub(r"\s+", " ", block).strip()
            for block in re.split(r"\n\s*\n", value)
            if block.strip()
        ]
        # Stub paragraphs under 8 words pass the 3-paragraph check but add
        # no value; drop them before keeping the first three.
        blocks = [block for block in blocks if len(block.split()) >= 8]
        value = "\n\n".join(blocks[:3])
        value = trim_to_word_limit(value, max_words=240)
    else:
        value = re.sub(r"\s+", " ", value).strip()
    return value.strip()


def validate_cover_letter(letter: dict[str, str]) -> None:
    joined = f"{letter['short_version']}\n{letter['full_version']}".lower()
    for banned in BANNED_PHRASES:
        if banned in joined:
            raise ValueError(f"cover letter contains banned phrasing: {banned}")
    if "\n- " in letter["full_version"] or letter["full_version"].lstrip().startswith("- "):
        raise ValueError("cover letter should not use bullet-list voice")
    paragraphs = [block for block in re.split(r"\n\s*\n", letter["full_version"]) if block.strip()]
    if len(paragraphs) < 3:
        raise ValueError("cover letter should contain three short paragraphs")


def generate_cover_letter(
    conn: sqlite3.Connection,
    company: str,
    role: str,
    job_url: str | None = None,
    tracker_row: dict[str, str] | None = None,
    jd_text: str | None = None,
    extra_notes: str | None = None,
) -> dict[str, str]:
    payload = build_cover_letter_payload(
        conn,
        company,
        role,
        job_url=job_url,
        tracker_row=tracker_row,
        jd_text=jd_text,
        extra_notes=extra_notes,
    )
    try:
        output_text = generate_text(
            SYSTEM_PROMPT_BASE, json.dumps(payload, ensure_ascii=False, indent=2)
        )
    except LLMClientError as exc:
        raise RuntimeError(f"AI request failed: {exc}") from exc
    if not output_text.strip():
        raise RuntimeError("AI backend returned an empty response")
    try:
        parsed = parse_cover_letter_response(output_text)
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"failed to parse AI response: {exc}") from exc
    letter = {
        "short_version": normalize_cover_letter_text(parsed["short_version"], is_full=False),
        "full_version": normalize_cover_letter_text(parsed["full_version"], is_full=True),
    }
    validate_cover_letter(letter)
    return letter


def render_cover_letter_markdown(
    company: str,
    role: str,
    job_url: str | None,
    short_version: str,
    full_version: str,
) -> str:
    lines = [
        "# Cover Letter",
        "",
        f"- Company: {company}",
        f"- Role: {role}",
    ]
    if job_url:
        lines.append(f"- Job URL: {job_url}")
    lines.extend(["", "## Short Version", short_version, "", "## Full Version", full_version, ""])
    return "\n".join(lines).rstrip() + "\n"


def markdown_to_html_paragraphs(text: str) -> str:
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    return "\n".join(f"<p>{html.escape(block)}</p>" for block in blocks)


def render_cover_letter_html(
    conn: sqlite3.Connection,
    company: str,
    role: str,
    full_version: str,
    template_dir: Path | None = None,
) -> str:
    directory = template_dir if template_dir is not None else TEMPLATE_DIR
    template = (directory / HTML_TEMPLATE).read_text(encoding="utf-8")
    css = (directory / CSS_TEMPLATE).read_text(encoding="utf-8")
    contact = candidate_contact(conn)
    html_text = template.replace(
        '<link rel="stylesheet" href="./cover-letter-template.css" />',
        f"<style>\n{css}\n</style>",
    )
    replacements = {
        "name": html.escape(contact["name"]),
        "headline": html.escape(role),
        "location": html.escape(contact["location"]),
        "email": html.escape(contact["email"]),
        "linkedin_url": html.escape(contact["linkedin_url"]),
        "linkedin_label": html.escape(contact["linkedin_label"]),
        "full_version_html": markdown_to_html_paragraphs(full_version),
    }
    for key, value in replacements.items():
        html_text = html_text.replace(f"{{{{{key}}}}}", value)
    unresolved = sorted(set(re.findall(r"{{([a-zA-Z0-9_]+)}}", html_text)))
    if unresolved:
        raise ValueError(f"unresolved cover letter template placeholders: {', '.join(unresolved)}")
    return html_text


def _default_render(html_text: str, pdf_path: Path) -> None:
    render_pdf(html_text, pdf_path, margin_mm=16)


def write_cover_letter_artifacts(
    conn: sqlite3.Connection,
    company: str,
    role: str,
    job_url: str | None,
    short_version: str,
    full_version: str,
    output_dir: Path | None = None,
    template_dir: Path | None = None,
    render: Callable[[str, Path], None] | None = None,
) -> dict[str, Path]:
    directory = output_dir if output_dir is not None else cover_letters_dir()
    directory.mkdir(parents=True, exist_ok=True)
    slug = slugify(f"{company}-{role}")
    markdown_path = directory / f"{slug}.md"
    html_path = directory / f"{slug}.html"
    pdf_path = directory / f"{slug}.pdf"

    markdown = render_cover_letter_markdown(company, role, job_url, short_version, full_version)
    html_text = render_cover_letter_html(conn, company, role, full_version, template_dir)
    markdown_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(html_text, encoding="utf-8")
    render_fn = render if render is not None else _default_render
    render_fn(html_text, pdf_path)
    if not pdf_path.exists():
        raise RuntimeError("PDF not created")
    return {"markdown": markdown_path, "html": html_path, "pdf": pdf_path}
