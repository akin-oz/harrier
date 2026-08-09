"""Outreach draft orchestration and the AI path (spec 017 port of
generate_outreach.py, openai_outreach.py, and outreach_lib.py's draft
rendering half).

Nothing sends. The AI prompt is persona-free: the candidate identity
rides in the payload from the profile store documents.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, cast

from harrier.apply.profile import (
    load_candidate_document,
    load_profile_json,
    load_profile_markdown,
)
from harrier.db import data_dir
from harrier.llm import LLMClientError, generate_text
from harrier.outreach.messages import (
    MESSAGE_KINDS,
    OutreachRequest,
    generate_message_bundle,
    load_configs,
    normalize,
    render_bundle_json,
    slugify,
)
from harrier.resume.content import load_truth_sources
from harrier.screening.descriptions import load_cached_description

logger = logging.getLogger(__name__)

RECRUITER_HINTS = ("recruit", "talent", "people", "sourc", "hr")
HIRING_MANAGER_HINTS = ("manager", "head", "director", "lead", "vp", "chief", "cto", "founder")
PEER_HINTS = (
    "engineer",
    "developer",
    "frontend",
    "web",
    "product engineer",
    "staff engineer",
    "software",
)

SHORT_NOTE_HARD_LIMIT = 280

OUTREACH_MESSAGE_TITLES = {
    "connection_note_short": "LinkedIn Connection Note (Under 300)",
    "connection_note_standard": "LinkedIn Connection Note",
    "follow_up_after_connection": "Follow-up After Connection",
    "follow_up_after_application_first": "First Follow-up After Applying",
    "follow_up_after_application_second": "Second Follow-up After Applying",
}

AI_SYSTEM_PROMPT = """You generate LinkedIn outreach messages for the candidate described in the payload's truth sources.

Write like a confident, senior engineer reaching out directly, NOT like a cover letter, NOT like a recruiter template. These messages appear in LinkedIn inboxes and need to feel human, specific, and worth replying to.

Core voice:
- conversational and direct
- confident but not arrogant
- specific: reference the company, the role, and why it is a genuine fit
- concise: every sentence earns its place
- human: reads like a real person wrote it quickly but thoughtfully
- no fluff, no filler, no corporate speak

You generate five message kinds. Each has a different purpose and length constraint:

1. connection_note_short: LinkedIn connection request. MUST be under 280 characters total. One punchy reason to connect. No greeting, no sign-off.
2. connection_note_standard: Slightly longer connection note. 2-3 sentences max. Brief intro plus a specific reason this connection makes sense.
3. follow_up_after_connection: Sent after they accept the connection. 3-4 sentences. Reference something specific and briefly mention relevant experience.
4. follow_up_after_application_first: First follow-up after applying. 3-4 sentences. Mention the application, add a concrete proof point not in the resume, and ask a genuine question about the team or product.
5. follow_up_after_application_second: Second (final) follow-up. 2-3 sentences. Light touch, no pressure.

For each message kind, generate 3 variants with different angles:
- v1: lead with a specific technical proof point
- v2: lead with genuine interest in the company/product
- v3: lead with a shared context or industry observation

Adapt tone and content based on the audience:
- recruiter: keep it simple, focus on fit and availability, mention key skills
- hiring_manager: be more technical, reference architecture decisions or team challenges
- peer: casual, reference shared technical interests, less formal

Hard constraints:
- Use ONLY facts from the supplied truth sources; never invent experience
- connection_note_short MUST be under 280 characters
- No "I am thrilled / passionate / excited"
- No "dream role" or "amazing opportunity"
- No flattery about the company being "incredible" or "world-class"
- No "I would be honored"
- No bullet lists
- No sign-offs; the platform handles that
- Do not repeat the same proof point across all variants
- If a job description is provided, reference specifics from it
- If the contact person's role is known, tailor the angle accordingly

Return strict JSON only:
{
  "connection_note_short": [
    {"variant_id": "v1", "text": "string"},
    {"variant_id": "v2", "text": "string"},
    {"variant_id": "v3", "text": "string"}
  ],
  "connection_note_standard": [...same structure...],
  "follow_up_after_connection": [...],
  "follow_up_after_application_first": [...],
  "follow_up_after_application_second": [...]
}

FORMATTING: Never use em dashes anywhere in the output. Use commas, semicolons, colons, or hyphens instead.
"""


def outreach_drafts_dir() -> Path:
    return data_dir() / "outreach"


def infer_audience(contact_role: str) -> str:
    role = normalize(contact_role)
    if not role:
        return "recruiter"
    if any(hint in role for hint in RECRUITER_HINTS):
        return "recruiter"
    if any(hint in role for hint in HIRING_MANAGER_HINTS):
        return "hiring_manager"
    if any(hint in role for hint in PEER_HINTS):
        return "peer"
    return "recruiter"


def load_job_description_text(job_url: str, jd_text: str = "") -> str:
    """The JD from an explicit argument or the description cache (stated
    change: no live enrichment fetch in the draft path)."""
    if jd_text.strip():
        return jd_text.strip()
    if not job_url.strip():
        return ""
    return load_cached_description(job_url).strip()


def build_payload(
    *,
    company: str,
    role: str,
    job_url: str,
    contact_name: str,
    contact_role: str,
    contact_linkedin: str,
    jd_text: str,
    audience: str = "",
    tone: str = "direct",
) -> dict[str, object]:
    resolved_audience = audience or infer_audience(contact_role)
    resolved_jd = load_job_description_text(job_url, jd_text)
    return {
        "company": company,
        "role": role,
        "job_url": job_url,
        "audience": resolved_audience,
        "tone": tone,
        "contact": {
            "name": contact_name,
            "role_title": contact_role,
            "linkedin_url": contact_linkedin,
        },
        "job_description_text": resolved_jd,
    }


# ---------------------------------------------------------------------------
# AI path
# ---------------------------------------------------------------------------


def _validate_short_notes(messages: dict[str, list[dict[str, Any]]]) -> None:
    for variant in messages.get("connection_note_short", []):
        text = str(variant.get("text", ""))
        if len(text) > SHORT_NOTE_HARD_LIMIT:
            variant["text"] = text[: SHORT_NOTE_HARD_LIMIT - 3].rsplit(" ", 1)[0] + "…"


def parse_ai_outreach_response(text: str) -> dict[str, list[dict[str, Any]]]:
    value = (text or "").strip()
    value = re.sub(r"^```(?:json)?\s*", "", value)
    value = re.sub(r"\s*```$", "", value).strip()
    start = value.find("{")
    end = value.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("AI response did not contain JSON")
    parsed: object = json.loads(value[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("expected a JSON object from the AI response")
    data = cast("dict[str, Any]", parsed)
    result: dict[str, list[dict[str, Any]]] = {}
    for kind in MESSAGE_KINDS:
        variants = data.get(kind)
        if not isinstance(variants, list) or not variants:
            raise ValueError(f"missing or empty message kind: {kind}")
        checked: list[dict[str, Any]] = []
        for raw in cast("list[object]", variants):
            if not isinstance(raw, dict) or not cast("dict[str, Any]", raw).get("text"):
                raise ValueError(f"invalid variant in {kind}")
            checked.append(cast("dict[str, Any]", raw))
        result[kind] = checked
    _validate_short_notes(result)
    return result


def generate_ai_outreach(
    conn: sqlite3.Connection,
    *,
    company: str,
    role: str,
    job_url: str = "",
    contact_name: str = "",
    contact_role: str = "",
    contact_linkedin: str = "",
    jd_text: str = "",
    audience: str = "recruiter",
    tone: str = "direct",
) -> dict[str, object]:
    sources = load_truth_sources(conn)
    payload = {
        "company": company,
        "role": role,
        "job_url": job_url,
        "audience": audience,
        "tone": tone,
        "contact": {
            "name": contact_name or "there",
            "role_title": contact_role,
            "linkedin_url": contact_linkedin,
        },
        "job_description_text": jd_text or "",
        "truth_sources": {
            "resume_truth_source_md": sources.truth_text,
            "latest_project_achievements_md": sources.achievements_text,
            "candidate_json": load_candidate_document(conn),
            "application_profile_md": load_profile_markdown(conn),
            "application_profile_json": load_profile_json(conn),
        },
    }
    try:
        output_text = generate_text(
            AI_SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False, indent=2)
        )
    except LLMClientError as exc:
        raise RuntimeError(f"AI request failed: {exc}") from exc
    if not output_text.strip():
        raise RuntimeError("AI backend returned an empty response")
    try:
        messages_raw = parse_ai_outreach_response(output_text)
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"failed to parse AI response: {exc}") from exc

    messages: dict[str, list[dict[str, Any]]] = {}
    selected: dict[str, str] = {}
    for kind in MESSAGE_KINDS:
        formatted: list[dict[str, Any]] = []
        for variant in messages_raw.get(kind, []):
            text = str(variant.get("text", "")).strip()
            formatted.append(
                {
                    "variant_id": variant.get("variant_id", "v1"),
                    "text": text,
                    "score": 90,
                    "char_count": len(text),
                    "flags": ["ai_generated"],
                }
            )
        messages[kind] = formatted
        if formatted:
            selected[kind] = str(formatted[0]["text"])

    return {
        "messages": messages,
        "selected_messages": selected,
        "job_description_text": jd_text,
        "recruiter_message": selected.get("connection_note_standard", ""),
        "hiring_manager_message": selected.get("connection_note_standard", ""),
        "follow_up_message": selected.get("follow_up_after_application_first", ""),
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def selected_messages(payload: dict[str, object]) -> dict[str, str]:
    messages = payload.get("messages")
    if not isinstance(messages, dict):
        return {}
    selected: dict[str, str] = {}
    for kind, variants in cast("dict[str, object]", messages).items():
        if not isinstance(variants, list):
            continue
        for raw in cast("list[object]", variants):
            if isinstance(raw, dict):
                text = str(cast("dict[str, object]", raw).get("text") or "").strip()
                if text:
                    selected[str(kind)] = text
                    break
    return selected


def build_request(payload: dict[str, object]) -> OutreachRequest:
    contact_raw = payload.get("contact")
    contact = cast("dict[str, object]", contact_raw) if isinstance(contact_raw, dict) else {}
    return OutreachRequest.from_dict(
        {
            "job_post_url": payload.get("job_url") or "https://local.invalid/outreach",
            "company_name": payload.get("company") or "",
            "role_title": payload.get("role") or "",
            "target_person_name": contact.get("name") or "there",
            "audience": payload.get("audience") or "recruiter",
            "tone": payload.get("tone") or "direct",
            "target_person_role": contact.get("role_title") or "",
            "linkedin_profile_url": contact.get("linkedin_url") or "",
        }
    )


def generate_outreach(
    conn: sqlite3.Connection,
    *,
    company: str,
    role: str,
    job_url: str = "",
    contact_name: str = "",
    contact_role: str = "",
    contact_linkedin: str = "",
    jd_text: str = "",
    audience: str = "",
    tone: str = "direct",
    ai: bool = False,
    config_dir: Path | None = None,
) -> dict[str, object]:
    resolved_audience = audience or infer_audience(contact_role)
    if ai:
        resolved_jd = load_job_description_text(job_url, jd_text)
        return generate_ai_outreach(
            conn,
            company=company,
            role=role,
            job_url=job_url,
            contact_name=contact_name,
            contact_role=contact_role,
            contact_linkedin=contact_linkedin,
            jd_text=resolved_jd,
            audience=resolved_audience,
            tone=tone,
        )

    payload = build_payload(
        company=company,
        role=role,
        job_url=job_url,
        contact_name=contact_name,
        contact_role=contact_role,
        contact_linkedin=contact_linkedin,
        jd_text=jd_text,
        audience=audience,
        tone=tone,
    )
    request = build_request(payload)
    bundle = generate_message_bundle(request, load_configs(conn, config_dir), variant_count=3)
    rendered: dict[str, object] = dict(render_bundle_json(request, bundle))
    rendered["selected_messages"] = selected_messages(rendered)
    rendered["job_description_text"] = str(payload.get("job_description_text") or "")
    picked = rendered["selected_messages"]
    assert isinstance(picked, dict)
    rendered["recruiter_message"] = picked.get("connection_note_standard", "")
    rendered["hiring_manager_message"] = picked.get("connection_note_standard", "")
    rendered["follow_up_message"] = picked.get("follow_up_after_application_first", "")
    return rendered


# ---------------------------------------------------------------------------
# Draft artifact rendering (outreach_lib.py port)
# ---------------------------------------------------------------------------


def _payload_sections(payload: dict[str, object]) -> list[dict[str, object]]:
    messages = payload.get("messages")
    if isinstance(messages, dict):
        sections: list[dict[str, object]] = []
        for kind, title in OUTREACH_MESSAGE_TITLES.items():
            raw_variants = cast("dict[str, object]", messages).get(kind)
            if not isinstance(raw_variants, list):
                continue
            variants: list[dict[str, object]] = []
            for index, raw_variant in enumerate(cast("list[object]", raw_variants), start=1):
                if not isinstance(raw_variant, dict):
                    continue
                entry = cast("dict[str, object]", raw_variant)
                text = str(entry.get("text") or "").strip()
                if not text:
                    continue
                variants.append(
                    {
                        "label": str(entry.get("variant_id") or f"v{index}"),
                        "text": text,
                        "score": entry.get("score", ""),
                        "char_count": entry.get("char_count", ""),
                        "flags": entry.get("flags", []),
                    }
                )
            if variants:
                sections.append({"key": kind, "title": title, "variants": variants})
        return sections

    legacy_sections: list[dict[str, object]] = []
    for key, title in (
        ("recruiter_message", "Recruiter Message"),
        ("hiring_manager_message", "Hiring Manager Message"),
        ("follow_up_message", "Follow-up Message"),
    ):
        text = str(payload.get(key) or "").strip()
        if text:
            legacy_sections.append(
                {
                    "key": key,
                    "title": title,
                    "variants": [
                        {"label": "v1", "text": text, "score": "", "char_count": "", "flags": []}
                    ],
                }
            )
    return legacy_sections


def _variant_meta_line(variant: dict[str, object]) -> str:
    flags_raw: object = variant.get("flags") or []
    flags = (
        [str(flag) for flag in cast("list[object]", flags_raw)]
        if isinstance(flags_raw, list)
        else []
    )
    return (
        f"- score: {variant.get('score') or 'n/a'} | chars: {variant.get('char_count') or 'n/a'} "
        f"| flags: {', '.join(flags) if flags else 'none'}"
    )


def render_outreach_markdown(payload: dict[str, object]) -> str:
    sections = _payload_sections(payload)
    if not sections:
        return ""
    lines: list[str] = []
    for section in sections:
        lines.append(f"# {section['title']}")
        lines.append("")
        variants_raw = section.get("variants")
        variants = (
            [
                cast("dict[str, object]", item)
                for item in cast("list[object]", variants_raw)
                if isinstance(item, dict)
            ]
            if isinstance(variants_raw, list)
            else []
        )
        primary = variants[0] if variants else {}
        lines.append(str(primary.get("text") or "").strip())
        if primary.get("char_count") or primary.get("score") or primary.get("flags"):
            lines.append("")
            lines.append(_variant_meta_line(primary))
        for variant in variants[1:]:
            lines.append("")
            lines.append(f"## Alternative {variant.get('label')}")
            lines.append("")
            lines.append(str(variant.get("text") or "").strip())
            lines.append("")
            lines.append(_variant_meta_line(variant))
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def write_outreach_draft(company: str, role: str, payload: dict[str, object]) -> dict[str, Path]:
    directory = outreach_drafts_dir()
    directory.mkdir(parents=True, exist_ok=True)
    slug = slugify(f"{company}-{role}")
    json_path = directory / f"{slug}.json"
    md_path = directory / f"{slug}.md"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_outreach_markdown(payload), encoding="utf-8")
    return {"json": json_path, "markdown": md_path}
