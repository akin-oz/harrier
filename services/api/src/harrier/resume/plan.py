"""The deterministic content plan and its validation (spec 013 port).

The plan is the authority for factual content. The optional model may only
reorder evidence the plan already approved (apply_ai_bullet_order); every
plan, AI-ordered or not, must pass validate_content_plan before rendering.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date

from harrier.resume.content import ResumeBundle, year_counts
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
from harrier.resume.vocabulary import SENIORITY_LEVELS

# Words that hedge a claim. The sentences the code writes carry none of them
# (spec 071 O10); verified bullets are quoted as they are.
HEDGE_WORDS = (
    "may",
    "might",
    "perhaps",
    "possibly",
    "somewhat",
    "fairly",
    "relatively",
    "arguably",
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
    # A comma, not a dash: the resume carries no dash as punctuation (spec 071).
    return f"{title}, {' & '.join(highlights)}" if highlights else title


def build_profile(bundle: ResumeBundle, skills: list[str], as_of: date | None = None) -> str:
    """Constrained presentation prose solely from canonical facts."""
    positioning = set(bundle.positioning_technologies)
    profile_skills = [skill for skill in skills if skill in positioning][:4]
    skill_phrase = _format_list(profile_skills or list(bundle.positioning_technologies[:3]))
    return " ".join(
        part for part in (profile_lead(bundle, skill_phrase, as_of), bundle.profile_summary) if part
    )


def profile_lead(bundle: ResumeBundle, skill_phrase: str, as_of: date | None = None) -> str:
    """The sentences of the profile the code writes. The rest is the
    bundle's `profile_summary`, candidate prose (persona-free engine)."""
    if bundle.experience_statement:
        # The operator's own wording replaces the derived phrase (spec 071 O7).
        statement = bundle.experience_statement.rstrip(".")
        return (
            f"{bundle.primary_identity} building user-facing web products with {skill_phrase}. "
            f"{statement[:1].upper()}{statement[1:]}."
        )
    experience = professional_experience_label(bundle, as_of)
    return (
        f"{bundle.primary_identity} with {experience} of professional experience building "
        f"user-facing web products with {skill_phrase}."
    )


def with_confirmed_skills(bundle: ResumeBundle, confirmed: tuple[str, ...]) -> ResumeBundle:
    """The bundle as one job sees it: skills the candidate confirmed in that
    job's brief count as verified there (spec 071 O9). The brief refuses a
    confirmed skill with no source when it is set, so none arrives here."""
    if not confirmed:
        return bundle
    added = tuple(skill for skill in confirmed if skill not in bundle.all_skills)
    return replace(
        bundle,
        all_skills=(*bundle.all_skills, *added),
        verified_skills=tuple(dict.fromkeys((*bundle.verified_skills, *confirmed)).keys()),
    )


def order_achievements_by_core(
    plan: ContentPlan, bundle: ResumeBundle, jd_text: str, requested_role: str
) -> ContentPlan:
    """Selected achievements led by core-requirement evidence (spec 071 O4),
    dated ones last (O5). Stable, so an earlier order, the model's included,
    only breaks ties."""
    # Imported here: evaluation reaches harrier.apply, which imports the
    # markdown writer, which imports this module.
    from harrier.resume.evaluation import core_support, dated_bullets

    selected = list(plan.selected_achievements)
    support = core_support(bundle, jd_text, requested_role, selected)
    dated = dated_bullets(bundle, jd_text, selected)
    ordered = sorted(
        selected,
        key=lambda item: (item in dated, -support[item][0], -support[item][1]),
    )
    return replace(plan, selected_achievements=ordered)


def build_content_plan(
    bundle: ResumeBundle, jd_text: str, requested_role: str, as_of: date | None = None
) -> ContentPlan:
    """Make a deterministic, evidence-backed resume content plan before
    rendering."""
    skills = rank_skills(bundle, jd_text, requested_role, as_of)
    # The full ach_ pool ranks best; a bundle using another naming scheme
    # still gets its validated default_achievements as the candidate pool.
    achievement_ids = [key for key in bundle.bullet_pool if key.startswith("ach_")] or list(
        bundle.default_achievements
    )
    from harrier.resume.evaluation import dated_bullets

    ranked_achievements = rank_bullet_ids(bundle, achievement_ids, jd_text, requested_role)
    # Dated achievements go last, so they fill a slot only when nothing else
    # qualifies (spec 071 O5).
    dated = dated_bullets(bundle, jd_text, ranked_achievements)
    ranked_achievements = [item for item in ranked_achievements if item not in dated] + [
        item for item in ranked_achievements if item in dated
    ]
    achievements = choose_distinct(bundle, ranked_achievements, 4)
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
        # choose_distinct already yields every group-distinct candidate up
        # to the count; refilling from the skipped remainder would reinsert
        # a duplicated evidence group and fail validate_content_plan, so a
        # shorter role section is accepted instead (review finding on PR #10).
        roles[role.id] = choose_distinct(bundle, candidates, role.bullet_count, achievement_groups)

    jd_scores = jd_technology_scores(bundle, jd_text, requested_role)
    primary_jd_skills = [skill for skill in skills if jd_scores.get(skill, 0)][:5]
    plan = ContentPlan(
        title=build_presentation_title(bundle, requested_role, jd_text, as_of),
        skills=skills,
        profile=build_profile(bundle, skills, as_of),
        role_bullets=roles,
        selected_achievements=achievements,
        primary_jd_skills=primary_jd_skills,
        role_periods={role.id: role_period_label(role, as_of) for role in bundle.roles},
        experience_label=professional_experience_label(bundle, as_of),
    )
    return order_achievements_by_core(plan, bundle, jd_text, requested_role)


def validate_content_plan(plan: ContentPlan, bundle: ResumeBundle) -> list[str]:
    """Validate factual grounding and tailoring quality before rendering."""
    errors: list[str] = []
    experience_text = bundle.experience_statement.rstrip(".") or plan.experience_label
    if experience_text.lower() not in plan.profile.lower():
        errors.append("profile experience length does not match canonical career start")
    computed_years = int(plan.experience_label.split("+", 1)[0])
    inflated = [count for count in year_counts(plan.profile) if count > computed_years]
    if inflated:
        errors.append(f"profile states {max(inflated)} years; the record holds {computed_years}")
    lead = plan.profile.removesuffix(bundle.profile_summary).strip()
    hedges = [word for word in HEDGE_WORDS if re.search(rf"\b{word}\b", lead, re.IGNORECASE)]
    if hedges:
        errors.append(f"profile lead hedges: {', '.join(hedges)}")
    held = " ".join([bundle.primary_identity, *(role.title for role in bundle.roles)]).lower()
    for level in SENIORITY_LEVELS:
        if re.search(rf"\b{level}\b", plan.title, re.IGNORECASE) and not re.search(
            rf"\b{level}\b", held
        ):
            errors.append(f"presentation title claims an unbacked {level} level")
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
