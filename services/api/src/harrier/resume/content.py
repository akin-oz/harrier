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

from harrier.resume.heading import role_heading, split_role_heading

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
class EducationEntry:
    """One degree as it appears on the resume (spec 059). Qualifiers such
    as part-time or an expected year live inside `degree`; order in the
    bundle is display order."""

    degree: str
    school: str


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
    education: tuple[EducationEntry, ...]
    certifications: tuple[str, ...]
    profile_summary: str = ""
    ownership_terms: tuple[str, ...] = field(
        default=("owned", "led", "designed", "established", "authored")
    )


def _is_single_line(value: str) -> bool:
    """Whether the parsers would read this value back as the one line it was
    written as.

    Asked of `str.splitlines()` itself, because that is what both the
    markdown validator and the HTML renderer split with, and it breaks on
    ten characters, not two (spec 062). A list of them kept here could only
    drift from the parser it describes. A trailing boundary fails too:
    `splitlines` drops it, so the value does not come back unchanged.
    """
    return value == "" or value.splitlines() == [value]


def _organization_survives_its_heading(organization: str) -> bool:
    """Whether the heading the writer builds for this role splits back into
    this organization (spec 062 Rule 3, asked of the one definition the
    writer and the parser share, spec 063).

    Containing a whole separator fails, and so does ending in its dash:
    the writer's own separator then makes a doubled one and the split falls
    a dash early. This used to re-enact the writer and the parser by hand,
    which was right only while neither of them changed.

    The heading is written with an empty title and no employment type,
    because neither can change the verdict: whatever follows the writer's
    own separator comes after it, and the split is on the first. So a role
    whose title is missing still has its organization checked, and one
    error names both problems.
    """
    emitted = organization.strip()
    return split_role_heading(role_heading(emitted, "", ""))[0] == emitted


def _starts_with_heading_marker(value: str) -> bool:
    return value.lstrip().startswith("#")


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
    bullet_count = 2
    if raw_count is not None:
        if isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count > 0:
            bullet_count = raw_count
        else:
            errors.append(f"{context}: bullet_count must be a positive integer, got {raw_count!r}")
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


def _parse_education(raw: object, errors: list[str]) -> tuple[EducationEntry, ...]:
    """Education is an ordered list of {degree, school} objects (spec 059).

    The old flat list of lines is refused by name rather than reinterpreted:
    the renderer used to keep its first two lines and silently drop the rest.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        errors.append("education must be a list of {degree, school} objects")
        return ()
    entries: list[EducationEntry] = []
    for index, item in enumerate(cast("list[object]", raw)):
        if not isinstance(item, dict):
            errors.append("education must be a list of {degree, school} objects")
            return ()
        entry = cast("dict[str, object]", item)
        values: dict[str, str] = {}
        for key in ("degree", "school"):
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"education[{index}] missing {key}")
                continue
            # The markdown resume is line-oriented and the HTML renderer
            # re-parses it, so a value that spans lines or opens with a
            # heading marker rewrites the document's structure: a line break
            # in `degree` closed the section and injected a certification
            # (review finding on PR #68).
            if not _is_single_line(value):
                errors.append(f"education[{index}] {key} must be a single line")
                continue
            if key == "school" and _starts_with_heading_marker(value):
                errors.append(f"education[{index}] school must not start with a heading marker")
                continue
            values[key] = value
        if len(values) == 2:
            entries.append(EducationEntry(degree=values["degree"], school=values["school"]))
    return tuple(entries)


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


def _check_emitted(
    path: str, value: object, errors: list[str], *, begins_unmarked_line: bool = False
) -> None:
    """One string `build_markdown` emits. Read as written, before any
    trimming, so a trailing line break is refused with the rest."""
    if not isinstance(value, str):
        return
    if not _is_single_line(value):
        errors.append(f"{path} must be a single line")
    if begins_unmarked_line and _starts_with_heading_marker(value):
        errors.append(f"{path} must not start with a heading marker")


def _check_emitted_list(
    path: str, value: object, errors: list[str], *, begins_unmarked_line: bool = False
) -> None:
    if not isinstance(value, list):
        return
    for index, item in enumerate(cast("list[object]", value)):
        _check_emitted(f"{path}[{index}]", item, errors, begins_unmarked_line=begins_unmarked_line)


def _check_markdown_structure(data: dict[str, object], errors: list[str]) -> None:
    """Every bundle string the resume markdown emits must be unable to edit
    that markdown's structure (spec 062).

    A line break anywhere ends the line the writer meant to write, and the
    renderer reads whatever follows as structure: in `profile_summary` it
    forged an achievements section the truth gate never saw. A value that
    begins a line with no writer marker in front of it must also not open
    with `#`. Values that always sit behind `# `, `### `, or `- `, or
    mid-line, keep that freedom, as an education `degree` does.

    Education is checked where it is parsed (spec 059).
    """
    candidate_raw = data.get("candidate")
    if isinstance(candidate_raw, dict):
        candidate = cast("dict[str, object]", candidate_raw)
        _check_emitted("candidate.name", candidate.get("name"), errors)
        _check_emitted("candidate.email", candidate.get("email"), errors)
        _check_emitted("candidate.linkedin", candidate.get("linkedin"), errors)
        # The contact line and the title line both start with these.
        _check_emitted(
            "candidate.location", candidate.get("location"), errors, begins_unmarked_line=True
        )
        _check_emitted(
            "candidate.primary_identity",
            candidate.get("primary_identity"),
            errors,
            begins_unmarked_line=True,
        )
        _check_emitted_list(
            "candidate.positioning_technologies",
            candidate.get("positioning_technologies"),
            errors,
        )
    _check_emitted("profile_summary", data.get("profile_summary"), errors)

    roles_raw = data.get("roles")
    if isinstance(roles_raw, list):
        for index, item in enumerate(cast("list[object]", roles_raw)):
            if not isinstance(item, dict):
                continue
            role = cast("dict[str, object]", item)
            for key in ("organization", "title", "employment_type"):
                _check_emitted(f"roles[{index}].{key}", role.get(key), errors)
            organization = role.get("organization")
            if isinstance(organization, str) and not _organization_survives_its_heading(
                organization
            ):
                errors.append(
                    f"roles[{index}].organization must not contain the title separator "
                    "or end with its dash"
                )

    # Any entry can rank first and so begin the skills line. `verified_skills`
    # needs no pass of its own: an entry missing from `all_skills` is already
    # refused, and one present is checked here.
    _check_emitted_list("all_skills", data.get("all_skills"), errors, begins_unmarked_line=True)
    _check_emitted_list(
        "certifications", data.get("certifications"), errors, begins_unmarked_line=True
    )
    pool_raw = data.get("bullet_pool")
    if isinstance(pool_raw, dict):
        for bullet_id, text in cast("dict[object, object]", pool_raw).items():
            _check_emitted(f"bullet_pool[{bullet_id}]", text, errors)


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
    # Planning treats these as mandatory; failing here keeps the diagnostic
    # next to the bundle instead of deep inside plan validation.
    if not verified_skills:
        errors.append("bundle has no verified_skills")
    unknown_verified = [skill for skill in verified_skills if skill not in all_skills]
    if unknown_verified:
        errors.append(
            "verified_skills entries missing from all_skills: " + ", ".join(unknown_verified)
        )

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
        education=_parse_education(data.get("education"), errors),
        certifications=_str_tuple(data.get("certifications")),
        profile_summary=str(data.get("profile_summary") or ""),
    )
    if not bundle.positioning_technologies:
        errors.append("candidate has no positioning_technologies")
    _check_markdown_structure(data, errors)
    if errors:
        raise ResumeBundleError("invalid resume content bundle: " + "; ".join(errors))
    return bundle


# Headings under which a truth document lists things that are NOT true: the
# candidate's own record of claims never to make. Substring containment
# validated every line under such a heading, so the document defeated itself
# (spec 034).
DISCLAIMER_HEADINGS = (
    "must not",
    "do not claim",
    "do not say",
    "never claim",
    "cannot claim",
    "forbidden",
    "not true",
    "untrue",
    "avoid claiming",
    "claims to avoid",
)

# Sentence openings that assert the opposite of what follows. A truth
# document saying a responsibility was not held validated the substring
# naming that responsibility.
NEGATIONS = (
    " did not ",
    " never ",
    " have not ",
    " has not ",
    " was not ",
    " were not ",
    " no longer ",
    " rather than ",
    " instead of ",
    " without ",
)


def _is_disclaimer_heading(line: str) -> bool:
    stripped = line.strip().lower().lstrip("#").strip()
    if not stripped:
        return False
    return any(marker in stripped for marker in DISCLAIMER_HEADINGS)


def _is_heading(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("#") or (stripped.endswith(":") and len(stripped) < 80)


def asserting_lines(document: str) -> list[str]:
    """The lines of a truth document that assert something true.

    Everything under a disclaimer heading is dropped, until the next heading.
    Without this a section headed "claims I must not make" verifies every
    claim listed under it, which is the opposite of what the document says.
    """
    kept: list[str] = []
    skipping = False
    for line in document.splitlines():
        if _is_heading(line):
            skipping = _is_disclaimer_heading(line)
            if skipping:
                continue
        if not skipping:
            kept.append(line)
    return kept


def _is_negated(line: str) -> bool:
    padded = f" {line.strip().lower()} "
    return any(marker in padded for marker in NEGATIONS)


@dataclass(frozen=True)
class TruthSources:
    truth_text: str
    achievements_text: str

    def _supporting(self) -> list[str]:
        """Every line that can verify a claim, from both documents.

        Computed per call rather than cached because the dataclass is frozen
        and the documents are small; correctness here matters more than the
        microseconds.
        """
        lines: list[str] = []
        for document in (self.truth_text, self.achievements_text):
            lines.extend(line for line in asserting_lines(document) if not _is_negated(line))
        return lines

    def contains(self, fragment: str) -> bool:
        """Whether the truth documents actually assert this.

        Three properties the previous `str.__contains__` did not have, each
        one a way a carefully written truth document defeated itself
        (spec 034):

        - **Structure.** Lines under a "claims I must not make" heading do not
          verify the claims they list.
        - **Polarity.** "I did not own the incident response rota" does not
          verify "own the incident response rota".
        - **Case.** A claim differing only in capitalisation is the same
          claim, and failing it silently dropped real evidence.
        """
        check = fragment.strip().rstrip(".").lower()
        if not check:
            return False
        return any(check in line.lower() for line in self._supporting())


def require_truth(sources: TruthSources, fragment: str) -> str:
    if not sources.contains(fragment):
        raise ResumeBundleError(f"missing verified fragment in truth sources: {fragment[:80]}")
    return fragment


def forbidden_hits(phrases: tuple[str, ...], text: str) -> list[str]:
    """Which of the candidate's own never-claim phrases appear in the text.

    This list was parsed, stored, exported and read by no validator. It is
    the candidate's own record of claims never to make, which makes it the
    highest-value check available and precisely the invention class a
    containment predicate cannot catch.
    """
    haystack = text.lower()
    return [phrase for phrase in phrases if phrase.strip() and phrase.strip().lower() in haystack]


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
