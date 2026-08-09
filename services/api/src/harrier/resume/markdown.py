"""Resume markdown assembly and final-content validation (spec 013 port).

Internal labels ("Tailored for X", company prefixes) are scrubbed from
the visible title; the header comes from the grounded content plan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from harrier.resume.content import ResumeBundle, TruthSources
from harrier.resume.facts import role_period_label
from harrier.resume.plan import ContentPlan, validate_content_plan

_TR_MAP = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")


def slugify(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", text.translate(_TR_MAP).lower()).strip("-")
    return re.sub(r"-{2,}", "-", value)


def normalize_visible_role_title(company: str, role: str) -> str:
    title = re.sub(r"^\s*Tailored for\s+", "", role, flags=re.IGNORECASE).strip()
    if company.strip():
        company_pattern = re.compile(
            rf"^\s*{re.escape(company.strip())}\s*(?:[—:\-|]\s*)+", flags=re.IGNORECASE
        )
        while True:
            updated = company_pattern.sub("", title).strip()
            if updated == title:
                break
            title = updated
    title = re.sub(r"\s*(?:\(\s*Remote\s*\)|[—-]\s*Remote)\s*$", "", title, flags=re.IGNORECASE)
    remote_suffixes = (
        r"Remote Europe|Remote EMEA|Remote EU|Remote UK/EU|Worldwide \(±?3 hours CET\)"
    )
    title = re.sub(
        rf"\s*-\s*({remote_suffixes})\s*$",
        r" — \1",
        title,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", title).strip(" -—:|")


def normalize_visible_url_text(url: str) -> str:
    visible = (url or "").strip()
    visible = re.sub(r"^https?://", "", visible, flags=re.IGNORECASE)
    return visible.rstrip("/")


@dataclass(frozen=True)
class RenderedResume:
    markdown: str
    plan: ContentPlan


def resolve_bullets(
    bundle: ResumeBundle, sources: TruthSources, ids: list[str]
) -> tuple[list[str], list[str]]:
    """Resolve IDs to text with a final truthfulness check; return (texts,
    omitted ids)."""
    texts: list[str] = []
    omitted: list[str] = []
    for bullet_id in ids:
        text = bundle.bullet_pool.get(bullet_id)
        if not text:
            omitted.append(bullet_id)
            continue
        if sources.contains(text):
            texts.append(text)
        else:
            omitted.append(bullet_id)
    return texts, omitted


def _section_bullets(
    bundle: ResumeBundle,
    sources: TruthSources,
    ids: list[str],
    fallback_ids: list[str],
    minimum: int = 2,
) -> list[str]:
    texts, _ = resolve_bullets(bundle, sources, ids)
    if len(texts) < minimum:
        texts, _ = resolve_bullets(bundle, sources, fallback_ids)
    return texts


def build_markdown(
    bundle: ResumeBundle,
    sources: TruthSources,
    plan: ContentPlan,
    as_of: date | None = None,
) -> str:
    """Assemble the resume markdown from a validated plan."""
    plan_errors = validate_content_plan(plan, bundle)
    if plan_errors:
        raise ValueError("invalid resume content plan: " + "; ".join(plan_errors))

    achievements = _section_bullets(
        bundle,
        sources,
        plan.selected_achievements,
        list(bundle.default_achievements),
    )
    lines = [
        f"# {bundle.name}",
        plan.title,
        f"{bundle.location} | {bundle.email} | {normalize_visible_url_text(bundle.linkedin)}",
        "",
        "## PROFILE",
        plan.profile,
        "",
        "## SELECTED ACHIEVEMENTS",
        *[f"- {bullet}" for bullet in achievements],
        "",
        "## EXPERIENCE",
        "",
    ]
    for role in bundle.roles:
        bullets = _section_bullets(
            bundle,
            sources,
            plan.role_bullets.get(role.id, []),
            list(role.default_bullets),
        )
        suffix = f" ({role.employment_type})" if role.employment_type else ""
        lines.extend(
            [
                f"### {role.organization} — {role.title}{suffix}",
                plan.role_periods[role.id],
                *[f"- {bullet}" for bullet in bullets],
                "",
            ]
        )
    lines.extend(
        [
            "## EDUCATION",
            *bundle.education,
            "",
            "## CERTIFICATIONS",
            *bundle.certifications,
            "",
            "## TECHNICAL SKILLS",
            ", ".join(plan.skills),
        ]
    )
    markdown = "\n".join(lines)
    markdown_errors = validate_rendered_markdown(markdown, plan, bundle)
    if markdown_errors:
        raise ValueError("invalid rendered resume content: " + "; ".join(markdown_errors))
    return markdown


def validate_rendered_markdown(markdown: str, plan: ContentPlan, bundle: ResumeBundle) -> list[str]:
    """Heuristic final-content checks after markdown assembly."""
    errors = validate_content_plan(plan, bundle)
    if "�" in markdown:
        errors.append("rendered resume contains replacement characters")
    if markdown.splitlines()[1] != plan.title:
        errors.append("rendered title differs from the grounded content plan")
    if plan.experience_label not in markdown:
        errors.append("rendered resume is missing the calculated experience length")
    for role in bundle.roles:
        if role_period_label(role) not in markdown:
            errors.append(f"rendered resume is missing canonical period for {role.id}")
    return errors


def build_internal_metadata(
    *,
    company: str,
    requested_role: str,
    visible_role_title: str,
    job_url: str,
    tracker_score: str,
    jd_source: str,
    plan: ContentPlan,
    fit_evaluation: dict[str, object] | None,
) -> dict[str, object]:
    """The sidecar only; nothing here may enter the visible artifact."""
    return {
        "company": company,
        "requested_role": requested_role,
        "visible_role_title": visible_role_title,
        "job_url": job_url,
        "tracker_score": tracker_score,
        "jd_source": jd_source,
        "ai_tailored": plan.ai_ordered,
        "selected_achievements": plan.selected_achievements,
        "role_bullets": plan.role_bullets,
        "fit_evaluation": fit_evaluation,
    }
