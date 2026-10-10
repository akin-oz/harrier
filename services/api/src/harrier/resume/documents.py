"""The two resume content documents and the table that divides them (spec 098).

`resume_facts` holds what is true about the candidate on every track.
`resume_framing` holds how the industry resume presents it. The bundle the
tailoring engine reads is the two merged, and is the same bundle the single
`resume_data` document used to give.

This module holds the table, the split of one document into two and their
merge back into one object. It reads no database and parses no bundle, so
`harrier.resume.content` can build on it.
"""

from __future__ import annotations

import re
from typing import cast

FACTS_KIND = "resume_facts"
FACTS_NAME = "resume-facts.json"
FRAMING_KIND = "resume_framing"
FRAMING_NAME = "industry.json"

# Every field the bundle has goes to exactly one document. `candidate` and
# `roles` are in both: each document holds its own keys of them, and roles
# are matched by `id`.
FACTS_CANDIDATE = (
    "name",
    "location",
    "email",
    "phone",
    "linkedin",
    "professional_career_start",
)
FRAMING_CANDIDATE = ("primary_identity", "positioning_technologies", "experience_statement")
FACTS_ROLE = (
    "id",
    "organization",
    "title",
    "employment_type",
    "period",
    "counts_towards_professional_experience",
    "technologies",
)
FRAMING_ROLE = ("id", "competencies", "bullet_count", "default_bullets")
FACTS_TOP = (
    "candidate",
    "roles",
    "education",
    "certifications",
    "all_skills",
    "verified_skills",
    "technology_aliases",
    "forbidden_phrases",
)
FRAMING_TOP = (
    "candidate",
    "profile_summary",
    "roles",
    "bullet_pool",
    "evidence_groups",
    "default_achievements",
    "target_signal_weights",
    "evaluation_dimensions",
)

_TABLES = {
    FACTS_KIND: (FACTS_TOP, FACTS_CANDIDATE, FACTS_ROLE),
    FRAMING_KIND: (FRAMING_TOP, FRAMING_CANDIDATE, FRAMING_ROLE),
}

# What a bundle error says when it is about a framing field. Every message
# `parse_bundle` writes names its field, or, for the bullet references, the
# pool or the dimension it failed against.
_FRAMING_MARKERS = (
    *FRAMING_CANDIDATE,
    *(key for key in FRAMING_TOP if key not in ("candidate", "roles")),
    *(key for key in FRAMING_ROLE if key != "id"),
    "default bullet",
    "default achievement",
    "dimension",
    "the pool",
)
_ROLE_INDEX = re.compile(r"^roles\[(\d+)\]")


def _is_comment(key: str) -> bool:
    return key.startswith("_")


def _other(kind: str) -> str:
    return FRAMING_KIND if kind == FACTS_KIND else FACTS_KIND


def split_document(
    raw: dict[str, object],
) -> tuple[dict[str, object], dict[str, object], list[str]]:
    """The facts and the framing of one `resume_data` object, each in the
    original's key order, and every key the table does not place.

    A `_` comment key goes to the facts. Nothing is dropped: a key the table
    does not place is returned, and the caller refuses the split.
    """
    facts: dict[str, object] = {}
    framing: dict[str, object] = {}
    unplaced: list[str] = []
    for key, value in raw.items():
        if _is_comment(key):
            facts[key] = value
        elif key == "candidate" and isinstance(value, dict):
            candidate = cast("dict[str, object]", value)
            fact_part, frame_part = _split_object(candidate, FACTS_CANDIDATE, FRAMING_CANDIDATE)
            facts[key], framing[key] = fact_part, frame_part
            unplaced.extend(_unplaced(candidate, FACTS_CANDIDATE + FRAMING_CANDIDATE, "candidate"))
        elif key == "roles" and isinstance(value, list):
            fact_roles: list[object] = []
            frame_roles: list[object] = []
            for index, role in enumerate(cast("list[object]", value)):
                if not isinstance(role, dict):
                    fact_roles.append(role)
                    continue
                role_dict = cast("dict[str, object]", role)
                fact_part, frame_part = _split_object(role_dict, FACTS_ROLE, FRAMING_ROLE)
                fact_roles.append(fact_part)
                frame_roles.append(frame_part)
                unplaced.extend(_unplaced(role_dict, FACTS_ROLE + FRAMING_ROLE, f"roles[{index}]"))
            facts[key], framing[key] = fact_roles, frame_roles
        elif key in FACTS_TOP:
            facts[key] = value
        elif key in FRAMING_TOP:
            framing[key] = value
        else:
            unplaced.append(key)
    return facts, framing, unplaced


def _split_object(
    source: dict[str, object], fact_keys: tuple[str, ...], frame_keys: tuple[str, ...]
) -> tuple[dict[str, object], dict[str, object]]:
    fact_part: dict[str, object] = {}
    frame_part: dict[str, object] = {}
    for key, value in source.items():
        if key in fact_keys or _is_comment(key):
            fact_part[key] = value
        if key in frame_keys:
            frame_part[key] = value
    return fact_part, frame_part


def _unplaced(source: dict[str, object], known: tuple[str, ...], path: str) -> list[str]:
    return [f"{path}.{key}" for key in source if key not in known and not _is_comment(key)]


def field_paths(document: dict[str, object]) -> list[str]:
    """The paths a document holds, for a report that prints no value."""
    paths: list[str] = []
    for key, value in document.items():
        if key == "candidate" and isinstance(value, dict):
            paths.extend(f"candidate.{name}" for name in cast("dict[str, object]", value))
        elif key == "roles" and isinstance(value, list):
            for index, role in enumerate(cast("list[object]", value)):
                if isinstance(role, dict):
                    paths.extend(
                        f"roles[{index}].{name}" for name in cast("dict[str, object]", role)
                    )
                else:
                    paths.append(f"roles[{index}]")
        else:
            paths.append(key)
    return paths


def misplaced_keys(kind: str, document: dict[str, object]) -> list[str]:
    """Every key in this document that the table puts in the other one."""
    top, candidate_keys, role_keys = _TABLES[kind]
    other_top, other_candidate, other_role = _TABLES[_other(kind)]
    errors: list[str] = []

    def refuse(path: str) -> None:
        errors.append(f"{kind}: {path} belongs in {_other(kind)}")

    for key, value in document.items():
        if key in other_top and key not in top:
            refuse(key)
        elif key == "candidate" and isinstance(value, dict):
            for name in cast("dict[str, object]", value):
                if name in other_candidate and name not in candidate_keys:
                    refuse(f"candidate.{name}")
        elif key == "roles" and isinstance(value, list):
            for index, role in enumerate(cast("list[object]", value)):
                if not isinstance(role, dict):
                    continue
                for name in cast("dict[str, object]", role):
                    if name in other_role and name not in role_keys:
                        refuse(f"roles[{index}].{name}")
    return errors


class MergeResult:
    """The merged object, the errors found merging it, and how a bundle
    error names its document."""

    def __init__(self, merged: dict[str, object], errors: list[str], framing_index: dict[int, int]):
        self.merged = merged
        self.errors = errors
        # Facts role index to framing role index, so a framing error about a
        # role names the role where the operator wrote it.
        self._framing_index = framing_index

    def name_document(self, error: str) -> str:
        """A bundle error, prefixed with the document its field is in."""
        if not any(marker in error for marker in _FRAMING_MARKERS):
            return f"{FACTS_KIND}: {error}"
        match = _ROLE_INDEX.match(error)
        if match is not None and int(match.group(1)) in self._framing_index:
            index = self._framing_index[int(match.group(1))]
            error = f"roles[{index}]" + error[match.end() :]
        return f"{FRAMING_KIND}: {error}"


def merge_documents(facts: dict[str, object], framing: dict[str, object]) -> MergeResult:
    """One bundle object from the two documents, for `parse_bundle`.

    A key in the wrong document, a framing role naming no facts role and a
    role framed twice are errors here. A facts role with no framing role
    keeps the parser's defaults for the fields it lacks.
    """
    errors = misplaced_keys(FACTS_KIND, facts) + misplaced_keys(FRAMING_KIND, framing)
    merged: dict[str, object] = {}
    for key, value in [*facts.items(), *framing.items()]:
        if key not in ("candidate", "roles") and not _is_comment(key):
            merged[key] = value

    fact_candidate = facts.get("candidate")
    frame_candidate = framing.get("candidate", {})
    if not isinstance(frame_candidate, dict):
        errors.append(f"{FRAMING_KIND}: candidate is not an object")
        frame_candidate = {}
    if isinstance(fact_candidate, dict):
        merged["candidate"] = {
            **cast("dict[str, object]", fact_candidate),
            **cast("dict[str, object]", frame_candidate),
        }
    elif fact_candidate is not None:
        merged["candidate"] = fact_candidate

    framed: dict[str, tuple[int, dict[str, object]]] = {}
    frame_roles = framing.get("roles", [])
    if not isinstance(frame_roles, list):
        errors.append(f"{FRAMING_KIND}: roles is not a list")
        frame_roles = []
    for index, role in enumerate(cast("list[object]", frame_roles)):
        role_id = cast("dict[str, object]", role).get("id") if isinstance(role, dict) else None
        if not isinstance(role_id, str) or not role_id.strip():
            errors.append(f"{FRAMING_KIND}: roles[{index}] has no id")
        elif role_id in framed:
            errors.append(f"{FRAMING_KIND}: roles[{index}] frames role {role_id} again")
        else:
            framed[role_id] = (index, cast("dict[str, object]", role))

    framing_index: dict[int, int] = {}
    fact_roles = facts.get("roles")
    if isinstance(fact_roles, list):
        roles: list[object] = []
        known: set[str] = set()
        for index, role in enumerate(cast("list[object]", fact_roles)):
            if not isinstance(role, dict):
                roles.append(role)
                continue
            fact_role = cast("dict[str, object]", role)
            role_id = fact_role.get("id")
            combined = dict(fact_role)
            if isinstance(role_id, str) and role_id in framed:
                known.add(role_id)
                frame_index, frame_role = framed[role_id]
                framing_index[index] = frame_index
                combined.update((k, v) for k, v in frame_role.items() if k != "id")
            roles.append(combined)
        merged["roles"] = roles
        for role_id, (index, _) in framed.items():
            if role_id not in known:
                errors.append(f"{FRAMING_KIND}: roles[{index}] names unknown role {role_id}")
    elif fact_roles is not None:
        merged["roles"] = fact_roles
    return MergeResult(merged, errors, framing_index)
