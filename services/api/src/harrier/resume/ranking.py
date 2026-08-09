"""JD relevance ranking over verified evidence (spec 013 port).

Aliases recognize a target technology in a JD; they never make that
technology candidate evidence. Support always comes from the bundle.
"""

from __future__ import annotations

import re
from datetime import date

from harrier.resume.content import ResumeBundle, ResumeRole
from harrier.resume.facts import role_end_date


def _occurrences(text: str, phrase: str) -> int:
    pattern = rf"(?<![a-z0-9]){re.escape(phrase.lower())}(?![a-z0-9])"
    return len(re.findall(pattern, text.lower()))


def jd_technology_scores(bundle: ResumeBundle, jd_text: str, role: str = "") -> dict[str, int]:
    """Score JD technology relevance without treating the JD as evidence."""
    source = f"{role}\n{jd_text}".lower()
    return {
        skill: sum(_occurrences(source, alias) for alias in aliases)
        for skill, aliases in bundle.technology_aliases.items()
    }


def _roles_with_technology(bundle: ResumeBundle, skill: str) -> list[ResumeRole]:
    return [role for role in bundle.roles if skill in role.technologies]


def _months_since(value: date, reference: date) -> int:
    return max(0, (reference.year - value.year) * 12 + reference.month - value.month)


def rank_skills(
    bundle: ResumeBundle, jd_text: str, role: str = "", as_of: date | None = None
) -> list[str]:
    """Order supported skills by target relevance, evidence depth, recency,
    and positioning."""
    reference = as_of or date.today()
    scores = jd_technology_scores(bundle, jd_text, role)
    positioning = set(bundle.positioning_technologies)
    supported = set(bundle.verified_skills or bundle.all_skills)
    original_order = {skill: index for index, skill in enumerate(bundle.all_skills)}
    ranked: list[tuple[int, int, str]] = []
    for skill in bundle.all_skills:
        if skill not in supported:
            continue
        roles = _roles_with_technology(bundle, skill)
        evidence_depth = len(roles)
        recency = 0
        if roles:
            latest = max((role_end_date(item) or reference) for item in roles)
            recency = max(0, 24 - _months_since(latest, reference))
        target_relevance = scores.get(skill, 0) * 100
        positioning_bonus = 18 if skill in positioning else 0
        score = target_relevance + evidence_depth * 12 + recency + positioning_bonus
        ranked.append((score, -original_order[skill], skill))
    return [skill for _, _, skill in sorted(ranked, reverse=True)]


def bullet_score(bundle: ResumeBundle, bullet_id: str, jd_text: str, role: str) -> int:
    """Favor concrete, target-relevant evidence over generic keyword matches."""
    bullet = bundle.bullet_pool[bullet_id]
    text = bullet.lower()
    source = f"{role}\n{jd_text}".lower()
    score = 0
    # Quantified means a real metric (percent, K-scale, or multi-digit),
    # not a bare version digit like "Vue 3" (review finding on PR #10).
    if re.search(r"(?:~?\d+[kK]|\d+%|\d{2,})", bullet):
        score += 100
    for aliases in bundle.technology_aliases.values():
        if any(_occurrences(source, alias) for alias in aliases) and any(
            _occurrences(text, alias) for alias in aliases
        ):
            score += 70
    for terms in bundle.target_signal_weights.values():
        if any(term in source for term in terms) and any(term in text for term in terms):
            score += 28
    # Concrete ownership and architecture language still beats a broad JD term.
    if any(term in text for term in bundle.ownership_terms):
        score += 18
    return score


def rank_bullet_ids(bundle: ResumeBundle, ids: list[str], jd_text: str, role: str) -> list[str]:
    order = {bullet_id: index for index, bullet_id in enumerate(ids)}
    return sorted(
        ids,
        key=lambda bullet_id: (-bullet_score(bundle, bullet_id, jd_text, role), order[bullet_id]),
    )


def choose_distinct(
    bundle: ResumeBundle,
    ids: list[str],
    count: int,
    excluded_groups: set[str] | None = None,
) -> list[str]:
    """Select up to count bullets whose evidence groups do not repeat.

    Evidence groups make semantic duplication detectable even when an
    achievement is a synthesized version of an experience bullet."""
    used_groups = set(excluded_groups or set())
    result: list[str] = []
    for bullet_id in ids:
        group = bundle.evidence_groups.get(bullet_id)
        if group and group in used_groups:
            continue
        result.append(bullet_id)
        if group:
            used_groups.add(group)
        if len(result) == count:
            break
    return result
