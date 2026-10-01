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
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from harrier.apply.answers import extract_json_object, slugify
from harrier.apply.brief import (
    EMPTY_BRIEF,
    Brief,
    Limits,
    brief_instructions,
    limit_violations,
    never_name_hits,
    with_operator_evidence,
)
from harrier.apply.claims import (
    Claim,
    ClaimCheckError,
    ClaimContext,
    NeedsInputError,
    banned_hits,
    check_claims,
    find_placeholders,
    parse_claims,
)
from harrier.apply.profile import (
    load_candidate_document,
    load_profile_json,
    load_profile_markdown,
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
from harrier.resume.markdown import normalize_visible_role_title, normalize_visible_url_text
from harrier.resume.pdf import render_pdf, render_validated_pdf, validate_rendered_pdf

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
- Use the application profile for positioning and safe framing only. Every
  fact about the candidate must come from resume_truth_source_md or
  latest_project_achievements_md.
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

Claims and evidence:
- Declare every factual sentence you write as a claim: the sentence exactly
  as it appears in your letter, whether it is about the candidate or the
  employer, and one or more evidence fragments.
- Evidence is quoted verbatim. Candidate evidence comes only from
  resume_truth_source_md or latest_project_achievements_md. Employer evidence
  comes only from job_description_text.
- Keep every number exactly as the evidence states it. A total over a period
  is never a rate, and a percentage keeps its sign.
- If the evidence describes a demo or synthetic data, say so in the sentence.
- Name a technology only if the truth sources show the candidate used it.
- If the letter needs a concrete example and none exists in the material,
  write [[TODO: what is needed]] instead of inventing one.
- Say "convention" or "warning" rather than "enforced" unless the material
  shows something enforcing it.
- Do not state model names, versions or other time-sensitive tool details
  unless the material supplies them.
- Write exactly 3 paragraphs of at least 8 words each and at most 240 words.

Return strict JSON only with this shape:
{
  "short_version": "string",
  "full_version": "string",
  "claims": [
    {"sentence": "string", "about": "candidate or employer", "evidence": ["string"]}
  ]
}

FORMATTING: Never use em dashes anywhere in the output. Use commas, semicolons, colons, or hyphens instead.
"""
)


def cover_letters_dir() -> Path:
    return data_dir() / "cover-letters"


def cover_letter_paths_for(
    company: str, role: str, output_dir: Path | None = None
) -> dict[str, Path]:
    """Where a cover letter run puts its files, by artifact kind.

    Shared by the writer below and by the reader that serves an artifact back,
    so the two cannot disagree about where a letter is (spec 047).
    """
    directory = output_dir if output_dir is not None else cover_letters_dir()
    slug = slugify(f"{company}-{role}")
    return {
        "markdown": directory / f"{slug}.md",
        "html": directory / f"{slug}.html",
        "pdf": directory / f"{slug}.pdf",
    }


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
    brief: Brief = EMPTY_BRIEF,
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
        # The application brief (spec 066). Empty values when there is none.
        "never_name": list(brief.never_name),
        "employer_guidance": brief.employer_guidance,
        "guidance_url": brief.guidance_url,
        "operator_evidence": list(brief.evidence),
        "truth_sources": {
            "resume_truth_source_md": sources.truth_text,
            "latest_project_achievements_md": sources.achievements_text,
            "candidate_json": candidate,
            "application_profile_md": load_profile_markdown(conn),
            "application_profile_json": load_profile_json(conn),
        },
    }


def parse_cover_letter_response(text: str) -> dict[str, str]:
    payload_raw: object = loads_tolerant(extract_json_object(text))
    payload = cast("dict[str, object]", payload_raw) if isinstance(payload_raw, dict) else {}
    short_version = str(payload.get("short_version", "")).strip()
    full_version = str(payload.get("full_version", "")).strip()
    if not short_version or not full_version:
        raise ValueError("AI response was missing cover letter fields")
    return {"short_version": short_version, "full_version": full_version}


def parse_cover_letter_claims(text: str) -> list[Claim]:
    """The claims the model declared alongside the letter (spec 065)."""
    payload_raw: object = loads_tolerant(extract_json_object(text))
    payload = cast("dict[str, object]", payload_raw) if isinstance(payload_raw, dict) else {}
    return parse_claims(payload.get("claims"))


def strip_banned_phrases(text: str) -> str:
    value = text
    for banned in BANNED_PHRASES:
        value = re.sub(re.escape(banned), "", value, flags=re.IGNORECASE)
    return value


MAX_WORDS = 240
PARAGRAPHS = 3
MIN_PARAGRAPH_WORDS = 8


def normalize_cover_letter_text(text: str, *, is_full: bool) -> str:
    """Formatting only: whitespace, leading bullet markers, and `Paragraph N:`
    labels (spec 065, rule N1).

    This used to delete banned phrases as substrings, drop short paragraphs,
    keep the first three, and trim to 240 words. Each of those changed what
    the letter said without saying so, and the first one cut words in half.
    They are now checks that refuse in `cover_letter_violations`.
    """
    value = (text or "").replace("\r\n", "\n")
    value = re.sub(r"^[\-\*•]\s+", "", value, flags=re.MULTILINE)
    value = re.sub(r"^\s*paragraph\s*\d+\s*:\s*", "", value, flags=re.IGNORECASE | re.MULTILINE)
    if is_full:
        blocks = [
            re.sub(r"\s+", " ", block).strip()
            for block in re.split(r"\n\s*\n", value)
            if block.strip()
        ]
        value = "\n\n".join(blocks)
    else:
        value = re.sub(r"\s+", " ", value).strip()
    return value.strip()


def cover_letter_violations(letter: dict[str, str], limits: Limits | None = None) -> list[str]:
    """Every shape and phrasing rule the letter breaks (spec 065, rule N1).

    A brief's stated limits replace the defaults and apply to the full
    letter, the version that is sent (spec 066, B2).
    """
    stated = limits or Limits()
    violations = [
        f"banned phrase: {phrase}"
        for phrase in banned_hits(
            BANNED_PHRASES, f"{letter['short_version']}\n{letter['full_version']}"
        )
    ]
    full = letter["full_version"]
    if "\n- " in full or full.lstrip().startswith("- "):
        violations.append("cover letter should not use bullet-list voice")
    paragraphs = [block for block in re.split(r"\n\s*\n", full) if block.strip()]
    if stated.paragraphs is None:
        if len(paragraphs) < PARAGRAPHS:
            violations.append("cover letter should contain three short paragraphs")
        if len(paragraphs) > PARAGRAPHS:
            violations.append(f"too many paragraphs: {len(paragraphs)}, at most {PARAGRAPHS}")
    else:
        if len(paragraphs) < stated.paragraphs:
            violations.append(
                f"cover letter should contain {stated.paragraphs} paragraphs (letter.paragraphs)"
            )
        if len(paragraphs) > stated.paragraphs:
            violations.append(
                f"over the stated limit: letter.paragraphs {stated.paragraphs},"
                f" full_version has {len(paragraphs)}"
            )
    for paragraph in paragraphs:
        if len(paragraph.split()) < MIN_PARAGRAPH_WORDS:
            violations.append(f"stub paragraph: {paragraph}")
    words = len(full.split())
    if stated.max_words is None and words > MAX_WORDS:
        violations.append(f"over the word limit: {words} words, at most {MAX_WORDS}")
    violations.extend(limit_violations(full, stated, "letter", "full_version"))
    return violations


@dataclass(frozen=True)
class LetterDraft:
    """A letter that passed every check, with the claims it rests on, which
    the draft's "To verify" section lists (spec 066, B9)."""

    short_version: str
    full_version: str
    claims: tuple[Claim, ...] = ()


def validate_cover_letter(letter: dict[str, str]) -> None:
    violations = cover_letter_violations(letter)
    if violations:
        raise ClaimCheckError(violations)


def generate_cover_letter(
    conn: sqlite3.Connection,
    company: str,
    role: str,
    job_url: str | None = None,
    tracker_row: dict[str, str] | None = None,
    jd_text: str | None = None,
    extra_notes: str | None = None,
    brief: Brief = EMPTY_BRIEF,
) -> LetterDraft:
    payload = build_cover_letter_payload(
        conn,
        company,
        role,
        job_url=job_url,
        tracker_row=tracker_row,
        jd_text=jd_text,
        extra_notes=extra_notes,
        brief=brief,
    )
    try:
        output_text = generate_text(
            SYSTEM_PROMPT_BASE + brief_instructions(brief, "letter"),
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
    except LLMClientError as exc:
        raise RuntimeError(f"AI request failed: {exc}") from exc
    if not output_text.strip():
        raise RuntimeError("AI backend returned an empty response")
    try:
        parsed = parse_cover_letter_response(output_text)
        claims = parse_cover_letter_claims(output_text)
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"failed to parse AI response: {exc}") from exc
    letter = {
        "short_version": normalize_cover_letter_text(parsed["short_version"], is_full=False),
        "full_version": normalize_cover_letter_text(parsed["full_version"], is_full=True),
    }
    texts = [letter["short_version"], letter["full_version"]]
    violations = cover_letter_violations(letter, brief.letter)
    violations.extend(
        f"named a redacted name: {name}"
        for name in never_name_hits(brief.never_name, "\n".join(texts))
    )
    # The candidate's own never-claim list. Spec 034 required it on the
    # letter; only the resume validator was calling it.
    violations.extend(
        f"forbidden phrase: {phrase}"
        for phrase in forbidden_hits(load_forbidden_phrases(conn), "\n".join(texts))
    )
    context = ClaimContext(
        sources=with_operator_evidence(load_truth_sources(conn), *brief.evidence),
        posting="\n".join(part for part in (jd_text or "", brief.employer_guidance) if part),
        company=company,
        role=role,
        vocabulary=load_skill_vocabulary(conn),
    )
    violations.extend(check_claims(texts, claims, context))
    if violations:
        raise ClaimCheckError(violations)
    return LetterDraft(
        short_version=letter["short_version"],
        full_version=letter["full_version"],
        claims=tuple(claims),
    )


def render_cover_letter_markdown(
    company: str,
    role: str,
    job_url: str | None,
    short_version: str,
    full_version: str,
    review_section: str = "",
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
    markdown = "\n".join(lines).rstrip() + "\n"
    return markdown + ("\n" + review_section if review_section else "")


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
        # Scrubbed and normalized like the resume header. The raw tracker
        # title was going straight into the letter, so an internal label or a
        # "Tailored for" prefix reached the recruiter (spec 034).
        "headline": html.escape(strip_banned_phrases(normalize_visible_role_title(company, role))),
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
    validate: Callable[[Path, str], list[str]] | None = None,
    review: Review | None = None,
) -> dict[str, Path]:
    directory = output_dir if output_dir is not None else cover_letters_dir()
    directory.mkdir(parents=True, exist_ok=True)
    paths = cover_letter_paths_for(company, role, directory)
    markdown_path = paths["markdown"]
    html_path = paths["html"]
    pdf_path = paths["pdf"]

    # The review sections go in the markdown draft only. The HTML and PDF are
    # built from full_version alone, so no flag reaches a recruiter (B10).
    review_section = render_review([short_version, full_version], review or Review(), markdown_path)
    markdown = render_cover_letter_markdown(
        company, role, job_url, short_version, full_version, review_section
    )
    markdown_path.write_text(markdown, encoding="utf-8")
    # The paths are per company and role, and the artifact endpoint serves
    # whatever PDF is there, so from here an earlier run's PDF and HTML would
    # sit beside this new draft as if they were current (review of #84). On
    # any failure the draft stays, for the operator to fix (spec 067).
    html_path.unlink(missing_ok=True)
    pdf_path.unlink(missing_ok=True)
    # A recruiter-facing artifact never carries a placeholder (spec 065,
    # rule C10).
    placeholders = find_placeholders(f"{short_version}\n{full_version}")
    if placeholders:
        raise NeedsInputError(markdown_path, placeholders)
    try:
        html_text = render_cover_letter_html(conn, company, role, full_version, template_dir)
        html_path.write_text(html_text, encoding="utf-8")
        # The resume path validated its PDF; this one only checked the file
        # existed, so a zero-byte or four-page letter passed. It is the one
        # recruiter-facing artifact that had neither gate (spec 034).
        pdf_errors = render_validated_pdf(
            html_text,
            pdf_path,
            render if render is not None else _default_render,
            validate if validate is not None else validate_rendered_pdf,
        )
        if pdf_errors:
            raise RuntimeError("invalid cover letter PDF: " + "; ".join(pdf_errors))
    except BaseException:
        html_path.unlink(missing_ok=True)
        raise
    return {"markdown": markdown_path, "html": html_path, "pdf": pdf_path}
