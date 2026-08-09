"""The deterministic content plan and its validation (spec 013 port).

The plan is the authority for factual content. The optional model may only
reorder evidence the plan already approved (apply_ai_bullet_order); every
plan, AI-ordered or not, must pass validate_content_plan before rendering.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from harrier.resume.content import ResumeBundle
from harrier.resume.facts import (
    professional_experience_label,
    role_end_date,
    role_period_label,
)
from harrier.resume.ranking import (
    choose_distinct,
    jd_technology_scores,
    rank_bullet_ids,
    rank_skills,
)


@dataclass
class ContentPlan:
    title: str
    skills: list[str]
    profile: str
    role_bullets: dict[str, list[str]]
    selected_achievements: list[str]
    primary_jd_skills: list[str]
    role_periods: dict[str, str]
    experience_label: str
    ai_ordered: bool = field(default=False)


def _format_list(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def is_full_stack_supported(bundle: ResumeBundle) -> bool:
    """Require substantial backend evidence, not a JD label or adjacent
    utility work."""
    backend_roles = sum("backend" in role.competencies for role in bundle.roles)
    full_stack_roles = sum("full-stack" in role.competencies for role in bundle.roles)
    return full_stack_roles > 0 or backend_roles >= 2


def build_presentation_title(
    bundle: ResumeBundle, requested_role: str, jd_text: str, as_of: date | None = None
) -> str:
    """Select a target-aware title that cannot promote unsupported identity
    claims."""
    requested = f"{requested_role}\n{jd_text}".lower()
    wants_full_stack = bool(re.search(r"full[-\s]?stack", requested))
    title = (
        "Senior Full-Stack Engineer"
        if wants_full_stack and is_full_stack_supported(bundle)
        else bundle.primary_identity
    )
    technology_scores = jd_technology_scores(bundle, jd_text, requested_role)
    highlights = [
        skill
        for skill in rank_skills(bundle, jd_text, requested_role, as_of)
        if technology_scores.get(skill, 0) and skill in bundle.positioning_technologies
    ][:2]
    return f"{title} — {' & '.join(highlights)}" if highlights else title


def build_profile(bundle: ResumeBundle, skills: list[str], as_of: date | None = None) -> str:
    """Constrained presentation prose solely from canonical facts."""
    positioning = set(bundle.positioning_technologies)
    profile_skills = [skill for skill in skills if skill in positioning][:4]
    skill_phrase = _format_list(profile_skills or list(bundle.positioning_technologies[:3]))
    experience = professional_experience_label(bundle, as_of)
    # The second sentence is candidate prose and comes from the bundle
    # (persona-free engine); the first is derived from facts.
    lead = (
        f"{bundle.primary_identity} with {experience} of professional experience building "
        f"user-facing web products with {skill_phrase}."
    )
    return f"{lead} {bundle.profile_summary}".strip() if bundle.profile_summary else lead


def build_content_plan(
    bundle: ResumeBundle, jd_text: str, requested_role: str, as_of: date | None = None
) -> ContentPlan:
    """Make a deterministic, evidence-backed resume content plan before
    rendering."""
    skills = rank_skills(bundle, jd_text, requested_role, as_of)
    achievement_ids = [key for key in bundle.bullet_pool if key.startswith("ach_")]
    achievements = choose_distinct(
        bundle, rank_bullet_ids(bundle, achievement_ids, jd_text, requested_role), 4
    )
    achievement_groups = {
        bundle.evidence_groups[item] for item in achievements if item in bundle.evidence_groups
    }
    roles: dict[str, list[str]] = {}
    for role in bundle.roles:
        candidates = rank_bullet_ids(
            bundle,
            [key for key in bundle.bullet_pool if key.startswith(f"{role.id}_")],
            jd_text,
            requested_role,
        )
        selected = choose_distinct(bundle, candidates, role.bullet_count, achievement_groups)
        fallback = [item for item in candidates if item not in selected]
        roles[role.id] = selected + fallback[: max(0, role.bullet_count - len(selected))]

    jd_scores = jd_technology_scores(bundle, jd_text, requested_role)
    primary_jd_skills = [skill for skill in skills if jd_scores.get(skill, 0)][:5]
    return ContentPlan(
        title=build_presentation_title(bundle, requested_role, jd_text, as_of),
        skills=skills,
        profile=build_profile(bundle, skills, as_of),
        role_bullets=roles,
        selected_achievements=achievements,
        primary_jd_skills=primary_jd_skills,
        role_periods={role.id: role_period_label(role, as_of) for role in bundle.roles},
        experience_label=professional_experience_label(bundle, as_of),
    )


def validate_content_plan(plan: ContentPlan, bundle: ResumeBundle) -> list[str]:
    """Validate factual grounding and tailoring quality before rendering."""
    errors: list[str] = []
    if plan.experience_label not in plan.profile:
        errors.append("profile experience length does not match canonical career start")
    if "full-stack" in plan.title.lower() and not is_full_stack_supported(bundle):
        errors.append("presentation title claims unsupported full-stack identity")
    unsupported = [skill for skill in plan.skills if skill not in bundle.verified_skills]
    if unsupported:
        errors.append(f"skills lack candidate evidence: {', '.join(unsupported)}")
    top_skills = set(plan.skills[:8])
    missing_primary = [skill for skill in plan.primary_jd_skills if skill not in top_skills]
    if missing_primary:
        errors.append(
            f"supported JD technologies are not visible near the top: {', '.join(missing_primary)}"
        )
    selected = [
        *plan.selected_achievements,
        *[item for values in plan.role_bullets.values() for item in values],
    ]
    if any(item not in bundle.bullet_pool for item in selected):
        errors.append("content plan contains an unknown bullet ID")
    groups = [bundle.evidence_groups[item] for item in selected if item in bundle.evidence_groups]
    if len(groups) != len(set(groups)):
        errors.append(
            "content plan duplicates the same evidence across achievements and experience"
        )
    expected_periods = {role.id: role_period_label(role) for role in bundle.roles}
    if plan.role_periods != expected_periods:
        errors.append("role dates do not match canonical engagement/role periods")
    for role in bundle.roles:
        if role_end_date(role) and "Present" in plan.role_periods[role.id]:
            errors.append(f"ended engagement {role.id} is rendered as Present")
    return errors


def apply_ai_bullet_order(
    plan: ContentPlan, bundle: ResumeBundle, ai_content: dict[str, list[str]] | None
) -> ContentPlan:
    """Let the model reorder planned evidence, never replace or add evidence."""
    if not ai_content:
        return plan
    roles = {key: list(value) for key, value in plan.role_bullets.items()}
    for position, role in enumerate(bundle.roles, start=1):
        requested_order = ai_content.get(f"role{position}_bullets")
        if requested_order is None:
            continue
        index = {bullet_id: rank for rank, bullet_id in enumerate(requested_order)}
        roles[role.id].sort(key=lambda bullet_id: index.get(bullet_id, len(index)))
    achievements = list(plan.selected_achievements)
    achievement_order = ai_content.get("selected_achievements")
    if achievement_order is not None:
        index = {bullet_id: rank for rank, bullet_id in enumerate(achievement_order)}
        achievements.sort(key=lambda bullet_id: index.get(bullet_id, len(index)))
    return ContentPlan(
        title=plan.title,
        skills=plan.skills,
        profile=plan.profile,
        role_bullets=roles,
        selected_achievements=achievements,
        primary_jd_skills=plan.primary_jd_skills,
        role_periods=plan.role_periods,
        experience_label=plan.experience_label,
        ai_ordered=True,
    )
