"""The resume content bundle: every candidate-specific fact the tailoring
engine consumes, loaded from the profile store (spec 013, ADR-008).

The old script hardcoded this content as module constants. The engine here
is persona-free: bullets, evidence groups, aliases, evaluation dimensions,
education, and contact fields all arrive as data. The repo commits only a
synthetic example bundle; the real bundle lives in the local database.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import cast

RESUME_DATA_KIND = "resume_data"
RESUME_TRUTH_KIND = "resume_truth"
ACHIEVEMENTS_KIND = "achievements"

DIMENSION_KINDS = ("default", "backend_ownership", "database", "absent_by_default")


class ResumeBundleError(ValueError):
    pass


@dataclass(frozen=True)
class ResumeRole:
    id: str
    organization: str
    title: str
    employment_type: str
    period_start: str
    period_end: str  # empty string means ongoing
    counts_towards_professional_experience: bool
    technologies: tuple[str, ...]
    competencies: tuple[str, ...]
    bullet_count: int
    default_bullets: tuple[str, ...]


@dataclass(frozen=True)
class EvaluationDimension:
    name: str
    signals: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    kind: str = "default"
    candidate_question: str = ""


@dataclass(frozen=True)
class ResumeBundle:
    name: str
    location: str
    email: str
    phone: str
    linkedin: str
    primary_identity: str
    professional_career_start: str
    positioning_technologies: tuple[str, ...]
    roles: tuple[ResumeRole, ...]
    all_skills: tuple[str, ...]
    verified_skills: tuple[str, ...]
    bullet_pool: dict[str, str]
    evidence_groups: dict[str, str]
    technology_aliases: dict[str, tuple[str, ...]]
    target_signal_weights: dict[str, tuple[str, ...]]
    evaluation_dimensions: tuple[EvaluationDimension, ...]
    forbidden_phrases: tuple[str, ...]
    default_achievements: tuple[str, ...]
    education: tuple[str, ...]
    certifications: tuple[str, ...]
    profile_summary: str = ""
    ownership_terms: tuple[str, ...] = field(
        default=("owned", "led", "designed", "established", "authored")
    )


def _require_str(source: dict[str, object], key: str, errors: list[str], context: str) -> str:
    value = source.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    errors.append(f"{context}: missing or empty {key}")
    return ""


def _str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in cast("list[object]", value) if isinstance(item, str))


def _str_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in cast("dict[str, object]", value).items()
        if isinstance(item, str)
    }


def _alias_dict(value: object) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        return {}
    return {str(key): _str_tuple(item) for key, item in cast("dict[str, object]", value).items()}


def _parse_role(raw: object, index: int, errors: list[str]) -> ResumeRole | None:
    if not isinstance(raw, dict):
        errors.append(f"roles[{index}]: not an object")
        return None
    role = cast("dict[str, object]", raw)
    context = f"roles[{index}]"
    role_id = _require_str(role, "id", errors, context)
    organization = _require_str(role, "organization", errors, context)
    title = _require_str(role, "title", errors, context)
    period = role.get("period")
    period_start = ""
    period_end = ""
    if isinstance(period, dict):
        period_dict = cast("dict[str, object]", period)
        start = period_dict.get("start")
        end = period_dict.get("end")
        period_start = start if isinstance(start, str) else ""
        period_end = end if isinstance(end, str) else ""
    if not period_start:
        errors.append(f"{context}: missing period.start")
    raw_count = role.get("bullet_count")
    bullet_count = raw_count if isinstance(raw_count, int) and raw_count > 0 else 2
    if role_id and organization and title and period_start:
        return ResumeRole(
            id=role_id,
            organization=organization,
            title=title,
            employment_type=str(role.get("employment_type") or ""),
            period_start=period_start,
            period_end=period_end,
            counts_towards_professional_experience=bool(
                role.get("counts_towards_professional_experience")
            ),
            technologies=_str_tuple(role.get("technologies")),
            competencies=_str_tuple(role.get("competencies")),
            bullet_count=bullet_count,
            default_bullets=_str_tuple(role.get("default_bullets")),
        )
    return None


def _parse_dimension(raw: object, index: int, errors: list[str]) -> EvaluationDimension | None:
    if not isinstance(raw, dict):
        errors.append(f"evaluation_dimensions[{index}]: not an object")
        return None
    entry = cast("dict[str, object]", raw)
    name = _require_str(entry, "name", errors, f"evaluation_dimensions[{index}]")
    kind_raw = entry.get("kind")
    kind = kind_raw if isinstance(kind_raw, str) and kind_raw else "default"
    if kind not in DIMENSION_KINDS:
        errors.append(f"evaluation_dimensions[{index}]: unknown kind {kind!r}")
        return None
    question = entry.get("candidate_question")
    if not name:
        return None
    return EvaluationDimension(
        name=name,
        signals=_str_tuple(entry.get("signals")),
        evidence_refs=_str_tuple(entry.get("evidence_refs")),
        kind=kind,
        candidate_question=question if isinstance(question, str) else "",
    )


def parse_bundle(raw: object) -> ResumeBundle:
    """Parse and validate the resume content bundle; raise ResumeBundleError
    with every problem named."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        raise ResumeBundleError("resume content bundle is not a JSON object")
    data = cast("dict[str, object]", raw)
    candidate_raw = data.get("candidate")
    candidate = cast("dict[str, object]", candidate_raw) if isinstance(candidate_raw, dict) else {}
    if not candidate:
        errors.append("missing candidate object")

    roles_raw = data.get("roles")
    roles: list[ResumeRole] = []
    if isinstance(roles_raw, list):
        for index, item in enumerate(cast("list[object]", roles_raw)):
            parsed = _parse_role(item, index, errors)
            if parsed is not None:
                roles.append(parsed)
    if not roles:
        errors.append("bundle has no valid roles")

    dimensions_raw = data.get("evaluation_dimensions")
    dimensions: list[EvaluationDimension] = []
    if isinstance(dimensions_raw, list):
        for index, item in enumerate(cast("list[object]", dimensions_raw)):
            parsed_dimension = _parse_dimension(item, index, errors)
            if parsed_dimension is not None:
                dimensions.append(parsed_dimension)

    bullet_pool = _str_dict(data.get("bullet_pool"))
    if not bullet_pool:
        errors.append("bundle has no bullet_pool")
    evidence_groups = _str_dict(data.get("evidence_groups"))
    for bullet_id in evidence_groups:
        if bullet_id not in bullet_pool:
            errors.append(f"evidence_groups references unknown bullet {bullet_id!r}")
    for role in roles:
        for bullet_id in role.default_bullets:
            if bullet_id not in bullet_pool:
                errors.append(f"role {role.id} default bullet {bullet_id!r} is not in the pool")
    default_achievements = _str_tuple(data.get("default_achievements"))
    for bullet_id in default_achievements:
        if bullet_id not in bullet_pool:
            errors.append(f"default achievement {bullet_id!r} is not in the pool")
    for dimension in dimensions:
        for ref in dimension.evidence_refs:
            if ref not in bullet_pool:
                errors.append(
                    f"dimension {dimension.name!r} evidence ref {ref!r} is not in the pool"
                )

    all_skills = _str_tuple(data.get("all_skills"))
    verified_skills = _str_tuple(data.get("verified_skills"))
    if not all_skills:
        errors.append("bundle has no all_skills ordering")

    bundle = ResumeBundle(
        name=_require_str(candidate, "name", errors, "candidate"),
        location=_require_str(candidate, "location", errors, "candidate"),
        email=_require_str(candidate, "email", errors, "candidate"),
        phone=str(candidate.get("phone") or ""),
        linkedin=_require_str(candidate, "linkedin", errors, "candidate"),
        primary_identity=_require_str(candidate, "primary_identity", errors, "candidate"),
        professional_career_start=_require_str(
            candidate, "professional_career_start", errors, "candidate"
        ),
        positioning_technologies=_str_tuple(candidate.get("positioning_technologies")),
        roles=tuple(roles),
        all_skills=all_skills,
        verified_skills=verified_skills,
        bullet_pool=bullet_pool,
        evidence_groups=evidence_groups,
        technology_aliases=_alias_dict(data.get("technology_aliases")),
        target_signal_weights=_alias_dict(data.get("target_signal_weights")),
        evaluation_dimensions=tuple(dimensions),
        forbidden_phrases=_str_tuple(data.get("forbidden_phrases")),
        default_achievements=default_achievements,
        education=_str_tuple(data.get("education")),
        certifications=_str_tuple(data.get("certifications")),
        profile_summary=str(data.get("profile_summary") or ""),
    )
    if errors:
        raise ResumeBundleError("invalid resume content bundle: " + "; ".join(errors))
    return bundle


@dataclass(frozen=True)
class TruthSources:
    truth_text: str
    achievements_text: str

    def contains(self, fragment: str) -> bool:
        check = fragment.rstrip(".")
        return check in self.truth_text or check in self.achievements_text


def require_truth(sources: TruthSources, fragment: str) -> str:
    if not sources.contains(fragment):
        raise ResumeBundleError(f"missing verified fragment in truth sources: {fragment[:80]}")
    return fragment


def _document_by_kind(conn: sqlite3.Connection, kind: str) -> str | None:
    row = conn.execute(
        "SELECT content FROM profile_documents WHERE kind = ? ORDER BY name LIMIT 1",
        (kind,),
    ).fetchone()
    return str(row[0]) if row is not None else None


def load_bundle(conn: sqlite3.Connection) -> ResumeBundle:
    content = _document_by_kind(conn, RESUME_DATA_KIND)
    if content is None:
        raise ResumeBundleError(
            "no resume_data document in the profile store; "
            "import one (see config/resume-content.example.json)"
        )
    try:
        raw: object = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ResumeBundleError(f"resume_data document is not valid JSON: {exc}") from exc
    return parse_bundle(raw)


def load_truth_sources(conn: sqlite3.Connection) -> TruthSources:
    truth = _document_by_kind(conn, RESUME_TRUTH_KIND)
    achievements = _document_by_kind(conn, ACHIEVEMENTS_KIND)
    if truth is None:
        raise ResumeBundleError("no resume_truth document in the profile store")
    return TruthSources(truth_text=truth, achievements_text=achievements or "")
