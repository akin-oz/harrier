"""The outreach template engine (spec 017 port of
outreach_messages_lib.py).

Templates, style rules, and role profiles are committed config; the
outreach defaults (candidate name, headline, strength phrases) come from
the profile store (ADR-008). Every message passes the lint-and-repair
loop; nothing here sends anything.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from harrier.db import data_dir
from harrier.profile.store import get_document

OUTREACH_CONFIG_DIR = Path("config") / "outreach"
OUTREACH_DEFAULTS_KIND = "outreach_defaults"
OUTREACH_DEFAULTS_NAME = "defaults.json"

SUPPORTED_AUDIENCES = {"recruiter", "hiring_manager", "peer"}
SUPPORTED_TONES = {"direct", "warm", "concise", "confident"}
MESSAGE_KINDS = [
    "connection_note_short",
    "connection_note_standard",
    "follow_up_after_connection",
    "follow_up_after_application_first",
    "follow_up_after_application_second",
]

CONNECTION_NOTE_LIMIT = 300


def slugify(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return re.sub(r"-{2,}", "-", value)


def normalize(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


@dataclass
class OutreachRequest:
    job_post_url: str
    company_name: str
    role_title: str
    target_person_name: str
    audience: str = "recruiter"
    tone: str = "direct"
    target_person_role: str = ""
    linkedin_profile_url: str = ""
    company_notes: str = ""
    custom_angle: str = ""
    sent_note_date: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> OutreachRequest:
        required = ["job_post_url", "company_name", "role_title", "target_person_name"]
        missing = [key for key in required if not str(payload.get(key, "")).strip()]
        if missing:
            raise ValueError(f"missing required fields: {', '.join(missing)}")
        audience = str(payload.get("audience") or "recruiter").strip().lower()
        tone = str(payload.get("tone") or "direct").strip().lower()
        if audience not in SUPPORTED_AUDIENCES:
            raise ValueError(f"unsupported audience: {audience}")
        if tone not in SUPPORTED_TONES:
            raise ValueError(f"unsupported tone: {tone}")
        return cls(
            job_post_url=str(payload.get("job_post_url") or "").strip(),
            company_name=str(payload.get("company_name") or "").strip(),
            role_title=str(payload.get("role_title") or "").strip(),
            target_person_name=str(payload.get("target_person_name") or "").strip(),
            audience=audience,
            tone=tone,
            target_person_role=str(payload.get("target_person_role") or "").strip(),
            linkedin_profile_url=str(payload.get("linkedin_profile_url") or "").strip(),
            company_notes=str(payload.get("company_notes") or "").strip(),
            custom_angle=str(payload.get("custom_angle") or "").strip(),
            sent_note_date=str(payload.get("sent_note_date") or "").strip(),
        )

    def slug(self) -> str:
        return slugify(
            f"{self.company_name}-{self.role_title}-{self.target_person_name}"
            f"-{self.audience}-{self.tone}"
        )


@dataclass
class MessageCheck:
    score: int
    flags: list[str]
    char_count: int


@dataclass
class MessageVariant:
    kind: str
    variant_id: str
    text: str
    check: MessageCheck


def _read_config(name: str, config_dir: Path | None = None) -> dict[str, Any]:
    directory = config_dir if config_dir is not None else OUTREACH_CONFIG_DIR
    parsed: object = json.loads((directory / name).read_text(encoding="utf-8"))
    return cast("dict[str, Any]", parsed) if isinstance(parsed, dict) else {}


def load_configs(conn: sqlite3.Connection, config_dir: Path | None = None) -> dict[str, Any]:
    """Committed template config plus the private defaults document."""
    defaults_raw = get_document(conn, OUTREACH_DEFAULTS_KIND, OUTREACH_DEFAULTS_NAME)
    if defaults_raw is None:
        raise ValueError(
            "no outreach_defaults document in the profile store; "
            "import one (see config/outreach-defaults.example.json)"
        )
    defaults: object = json.loads(defaults_raw)
    return {
        "defaults": cast("dict[str, Any]", defaults) if isinstance(defaults, dict) else {},
        "role_profiles": _read_config("role-profiles.json", config_dir),
        "style_rules": _read_config("style-rules.json", config_dir),
        "templates": _read_config("templates.json", config_dir),
    }


def join_phrases(parts: list[str]) -> str:
    clean = [part.strip() for part in parts if part and part.strip()]
    if not clean:
        return ""
    if len(clean) == 1:
        return clean[0]
    if len(clean) == 2:
        return f"{clean[0]} and {clean[1]}"
    return f"{', '.join(clean[:-1])}, and {clean[-1]}"


def first_name(full_name: str) -> str:
    text = (full_name or "").strip()
    return text.split()[0] if text else "there"


def shorten_text(text: str, limit_words: int = 14) -> str:
    words = re.split(r"\s+", (text or "").strip())
    if len(words) <= limit_words:
        return " ".join(words).strip()
    return " ".join(words[:limit_words]).strip()


def ensure_sentence(text: str) -> str:
    cleaned = " ".join((text or "").strip().split())
    if not cleaned:
        return ""
    if cleaned[-1] not in ".!?":
        cleaned += "."
    return cleaned


def clean_message(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text.strip())
    cleaned = re.sub(r"\s+([,.!?])", r"\1", cleaned)
    cleaned = re.sub(r"\.\s+\.", ".", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip()


def significant_role_tokens(role_title: str) -> list[str]:
    stop = {
        "senior",
        "staff",
        "engineer",
        "software",
        "developer",
        "remote",
        "in",
        "the",
        "and",
        "or",
    }
    tokens = re.findall(r"[a-z0-9]+", normalize(role_title))
    return [token for token in tokens if token not in stop]


def resolve_role_profile(role_title: str, role_profiles: dict[str, Any]) -> dict[str, Any]:
    normalized_title = normalize(role_title)
    for profile in role_profiles.get("profiles", []):
        if any(normalize(term) in normalized_title for term in profile.get("match_any", [])):
            return profile
    return role_profiles.get("fallback", {})


def strength_phrases(profile: dict[str, Any], defaults: dict[str, Any]) -> tuple[str, str]:
    strength_map = defaults.get("strengths", {})
    phrases = [str(strength_map.get(key) or "").strip() for key in profile.get("strength_keys", [])]
    phrases = [phrase for phrase in phrases if phrase]
    short = join_phrases(phrases[:2] or phrases[:1])
    long = join_phrases(phrases[:3] or phrases[:2] or phrases[:1])
    return short, long


def base_fields(
    request: OutreachRequest, configs: dict[str, Any], variant_index: int
) -> dict[str, str]:
    defaults = configs["defaults"]
    style_rules = configs["style_rules"]
    tone_rules = style_rules["tone_presets"][request.tone]
    profile = resolve_role_profile(request.role_title, configs["role_profiles"])
    short_strength, long_strength = strength_phrases(profile, defaults)
    angle_lines = profile.get("angle_lines", []) or configs["role_profiles"].get(
        "fallback", {}
    ).get("angle_lines", [])
    angle_sentence = (
        str(angle_lines[variant_index % len(angle_lines)])
        if angle_lines
        else "clear ownership and maintainable delivery"
    )
    role_labels = profile.get("role_labels", [])
    angle_short = join_phrases(role_labels[:2] or [angle_sentence])
    company_note_sentence = ""
    if request.company_notes:
        company_note_sentence = ensure_sentence(shorten_text(request.company_notes, limit_words=18))
    custom_angle_sentence = ""
    if request.custom_angle:
        custom_angle_sentence = ensure_sentence(shorten_text(request.custom_angle, limit_words=18))
    return {
        "greeting": tone_rules["greeting"].format(
            first_name=first_name(request.target_person_name)
        ),
        "connection_close": tone_rules["connection_close"],
        "standard_close": tone_rules["standard_close"],
        "follow_up_close": tone_rules["follow_up_close"],
        "peer_close": tone_rules["peer_close"],
        "company": request.company_name,
        "role_title": request.role_title,
        "first_name": first_name(request.target_person_name),
        "person_name": request.target_person_name,
        "person_role": request.target_person_role,
        "job_post_url": request.job_post_url,
        "linkedin_profile_url": request.linkedin_profile_url,
        "short_strength": short_strength,
        "strength_sentence": long_strength,
        "angle_sentence": angle_sentence,
        "angle_short": angle_short,
        "company_note_sentence": company_note_sentence,
        "custom_angle_sentence": custom_angle_sentence,
    }


def render_template(template: str, fields: dict[str, str]) -> str:
    rendered = template.format_map(defaultdict(str, fields))
    return clean_message(rendered)


def trim_linkedin_note(text: str, limit: int, style_rules: dict[str, Any]) -> str:
    trimmed = text
    if len(trimmed) <= limit:
        return trimmed
    for phrase in style_rules.get("trim_phrases", []):
        trimmed = re.sub(rf"\b{re.escape(phrase)}\b", "", trimmed, flags=re.IGNORECASE)
        trimmed = clean_message(trimmed)
        if len(trimmed) <= limit:
            return trimmed
    sentences = [piece.strip() for piece in re.split(r"(?<=[.!?])\s+", trimmed) if piece.strip()]
    while len(" ".join(sentences)) > limit and len(sentences) > 1:
        sentences.pop()
    trimmed = clean_message(" ".join(sentences))
    if len(trimmed) <= limit:
        return trimmed
    return trimmed[: limit - 1].rstrip(" ,;:-") + "…"


def check_message(
    text: str, request: OutreachRequest, kind: str, configs: dict[str, Any]
) -> MessageCheck:
    style_rules = configs["style_rules"]
    lowered = normalize(text)
    score = 100
    flags: list[str] = []

    if kind == "connection_note_short" and len(text) > CONNECTION_NOTE_LIMIT:
        score -= 25
        flags.append("too long for LinkedIn note")

    if request.company_name.lower() not in lowered:
        score -= 12
        flags.append("missing company reference")

    role_tokens = significant_role_tokens(request.role_title)
    if role_tokens and not any(token in lowered for token in role_tokens):
        score -= 15
        flags.append("weak role alignment")

    if not any(
        token in lowered
        for token in [
            "typescript",
            "frontend",
            "product",
            "architecture",
            "performance",
            "reliability",
            "ui",
            "systems",
        ]
    ):
        score -= 12
        flags.append("too generic")

    for phrase in style_rules.get("flattery_phrases", []):
        if phrase in lowered:
            score -= 15
            flags.append("too much flattery")
            break

    for phrase in style_rules.get("generic_phrases", []):
        if phrase in lowered:
            score -= 10
            flags.append("generic phrasing")
            break

    for phrase in style_rules.get("vague_phrases", []):
        if phrase in lowered:
            score -= 8
            flags.append("vague wording")
            break

    for term in style_rules.get("banned_terms", []):
        if term in lowered:
            score -= 20
            flags.append(f"banned term: {term}")
            break

    return MessageCheck(score=max(0, min(100, score)), flags=flags, char_count=len(text))


def rewrite_message(text: str, request: OutreachRequest, kind: str, configs: dict[str, Any]) -> str:
    style_rules = configs["style_rules"]
    replacements = {
        "wanted to reach out": "following up",
        "thought i would connect": "thought it made sense to connect",
        "i believe i would be a great fit": "the role looks aligned from my side",
        "value add": "relevant context",
        "passionate": "focused",
        "excited": "interested",
        "thrilled": "interested",
        "journey": "work",
        "perfect fit": "strong match",
        "rockstar": "engineer",
        "ninja": "engineer",
    }
    rewritten = clean_message(text)
    for source, target in replacements.items():
        rewritten = re.sub(rf"\b{re.escape(source)}\b", target, rewritten, flags=re.IGNORECASE)
    rewritten = clean_message(rewritten)
    if request.company_name.lower() not in normalize(rewritten):
        rewritten = clean_message(f"{rewritten} at {request.company_name}")
    if kind == "connection_note_short":
        rewritten = trim_linkedin_note(rewritten, CONNECTION_NOTE_LIMIT, style_rules)
    return rewritten


def generate_variants(
    request: OutreachRequest, kind: str, configs: dict[str, Any], variant_count: int = 3
) -> list[MessageVariant]:
    templates = configs["templates"][kind][request.audience]
    style_rules = configs["style_rules"]
    variants: list[MessageVariant] = []
    for index in range(min(variant_count, len(templates))):
        fields = base_fields(request, configs, index)
        text = render_template(templates[index], fields)
        if kind == "connection_note_short":
            text = trim_linkedin_note(text, CONNECTION_NOTE_LIMIT, style_rules)
        text = rewrite_message(text, request, kind, configs)
        check = check_message(text, request, kind, configs)
        variants.append(
            MessageVariant(kind=kind, variant_id=f"v{index + 1}", text=text, check=check)
        )
    return variants


def generate_message_bundle(
    request: OutreachRequest, configs: dict[str, Any], variant_count: int = 3
) -> dict[str, list[MessageVariant]]:
    return {
        kind: generate_variants(request, kind, configs, variant_count=variant_count)
        for kind in MESSAGE_KINDS
    }


def render_bundle_markdown(
    request: OutreachRequest, bundle: dict[str, list[MessageVariant]]
) -> str:
    lines = [
        "# Outreach Messages",
        "",
        f"- Company: {request.company_name}",
        f"- Role: {request.role_title}",
        f"- Audience: {request.audience}",
        f"- Tone: {request.tone}",
        f"- Target person: {request.target_person_name}",
        f"- Job URL: {request.job_post_url}",
        "",
    ]
    for kind in MESSAGE_KINDS:
        title = kind.replace("_", " ").title()
        lines.extend([f"## {title}", ""])
        for variant in bundle.get(kind, []):
            flags = ", ".join(variant.check.flags) if variant.check.flags else "none"
            lines.append(f"### {variant.variant_id}")
            lines.append(variant.text)
            lines.append("")
            lines.append(
                f"- score: {variant.check.score} | chars: {variant.check.char_count} "
                f"| flags: {flags}"
            )
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_bundle_json(
    request: OutreachRequest, bundle: dict[str, list[MessageVariant]]
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "request": asdict(request),
        "messages": {
            kind: [
                {
                    "variant_id": variant.variant_id,
                    "text": variant.text,
                    "score": variant.check.score,
                    "char_count": variant.check.char_count,
                    "flags": variant.check.flags,
                }
                for variant in variants
            ]
            for kind, variants in bundle.items()
        },
    }


def messages_dir() -> Path:
    return data_dir() / "outreach" / "messages"


def save_generated_bundle(
    request: OutreachRequest, bundle: dict[str, list[MessageVariant]]
) -> dict[str, Path]:
    directory = messages_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stem = request.slug()
    json_path = directory / f"{stem}.json"
    md_path = directory / f"{stem}.md"
    json_path.write_text(
        json.dumps(render_bundle_json(request, bundle), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    md_path.write_text(render_bundle_markdown(request, bundle), encoding="utf-8")
    return {"json": json_path, "markdown": md_path}


def target_store_path() -> Path:
    return data_dir() / "outreach" / "targets.json"


def load_target_store() -> list[dict[str, Any]]:
    path = target_store_path()
    if not path.exists():
        return []
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    return [
        cast("dict[str, Any]", item)
        for item in cast("list[object]", payload)
        if isinstance(item, dict)
    ]


def save_target_store(items: list[dict[str, Any]]) -> None:
    path = target_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")


def save_target(request: OutreachRequest) -> dict[str, Any]:
    items = load_target_store()
    payload: dict[str, Any] = asdict(request)
    payload["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    key = (
        normalize(request.company_name),
        normalize(request.role_title),
        normalize(request.target_person_name),
        normalize(request.linkedin_profile_url),
    )
    for existing in items:
        existing_key = (
            normalize(str(existing.get("company_name") or "")),
            normalize(str(existing.get("role_title") or "")),
            normalize(str(existing.get("target_person_name") or "")),
            normalize(str(existing.get("linkedin_profile_url") or "")),
        )
        if existing_key == key:
            existing.update(payload)
            save_target_store(items)
            return existing
    items.append(payload)
    save_target_store(items)
    return payload
