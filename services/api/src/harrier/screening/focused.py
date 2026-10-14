"""A focused track's screening document and its score (spec 137).

A focused track searches for roles with a bounded scope. It owns one profile
document of kind `screening`: the targets, the scoring weights, the bonus and
penalty groups, and the ownership signals spec 139 places postings by. This
module parses that document and scores a posting with it.

The gates are the industry kind's, read from `harrier.screening.rules`
unchanged: the title rules over the document's targets, and the remote-only
and EMEA gate over the shared candidate configuration's regions. A manual add
lands whatever its gates say, as it does on the industry track; the failed
gate is named in the row's signals.

Penalties rank and never reject. A posting with a blocker loses the derived
blocker penalty, so it ranks below every posting without one, as on the
industry track (specs 078, 081).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from harrier.connection import Connection
from harrier.profile.store import owned_documents
from harrier.screening.normalized import NormalizedJob, normalize
from harrier.screening.rules import (
    PREFERRED_REGION_PATTERNS,
    REMOTE_POSITIVE_PATTERNS,
    CandidateConfig,
    blockers,
    contains_word,
    is_target_title_variant,
    remote_region_allowed,
    text_matches_any_pattern,
    title_allowed,
)
from harrier.tracks import Scope

SCREENING_KIND = "screening"
SCREENING_NAME = "screening.json"

SCORER = "scorer=focused"
FIELDS: tuple[str, ...] = ("title", "description")

_TARGET_KEYS: tuple[str, ...] = ("titles", "title_keywords_include", "title_keywords_exclude")
_SCORING_KEYS: tuple[str, ...] = (
    "base_score",
    "exact_title_bonus",
    "include_keyword_bonus",
    "include_keyword_bonus_cap",
    "remote_bonus",
    "preferred_region_bonus",
)
_GROUP_KEYS: tuple[str, ...] = ("name", "phrases", "fields", "points")
_OWNERSHIP_KEYS: tuple[str, ...] = ("broad_title_signals", "broad_description_signals")
_TOP_KEYS: tuple[str, ...] = ("targets", "scoring", "bonuses", "penalties", "ownership")


class ScreeningDocumentError(ValueError):
    """The document breaks a rule. `path` names the first field that does,
    such as `penalties[2].points`, so the operator can find it."""

    def __init__(self, path: str, problem: str) -> None:
        self.path = path
        self.problem = problem
        super().__init__(f"{path}: {problem}")


@dataclass(frozen=True)
class PhraseGroup:
    name: str
    phrases: tuple[str, ...]
    fields: tuple[str, ...]
    points: int


@dataclass(frozen=True)
class ScreeningDocument:
    titles: tuple[str, ...]
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    scoring: Mapping[str, int]
    bonuses: tuple[PhraseGroup, ...]
    penalties: tuple[PhraseGroup, ...]
    broad_title_signals: tuple[str, ...]
    broad_description_signals: tuple[str, ...]
    # The parsed document as plain data, which the policy version digests.
    fingerprint: Mapping[str, Any]

    def targets_config(self) -> CandidateConfig:
        """The document's targets in the shape the industry title rule reads."""
        return {
            "targets": {
                "titles": list(self.titles),
                "title_keywords_include": list(self.include),
                "title_keywords_exclude": list(self.exclude),
            }
        }


@dataclass(frozen=True)
class FocusedScore:
    score: int
    reasons: list[str]
    version: str


# --- parsing -------------------------------------------------------------------


def _object(value: object, path: str, keys: tuple[str, ...]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ScreeningDocumentError(path, "must be an object")
    found = cast("dict[str, object]", value)
    for key in found:
        if key not in keys:
            raise ScreeningDocumentError(f"{path}.{key}" if path else key, "is not a known key")
    for key in keys:
        if key not in found:
            raise ScreeningDocumentError(f"{path}.{key}" if path else key, "is missing")
    return found


def _strings(value: object, path: str, *, non_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ScreeningDocumentError(path, "must be a list of strings")
    items = cast("list[object]", value)
    if non_empty and not items:
        raise ScreeningDocumentError(path, "must not be empty")
    out: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, str) or not item.strip():
            raise ScreeningDocumentError(f"{path}[{index}]", "must be a non-empty string")
        out.append(item.strip())
    return tuple(out)


def _integer(value: object, path: str, *, positive: bool = False) -> int:
    # bool is an int in Python; `true` is not a weight.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScreeningDocumentError(path, "must be an integer")
    if positive and value <= 0:
        raise ScreeningDocumentError(path, "must be a positive integer")
    return value


def _groups(value: object, path: str) -> tuple[PhraseGroup, ...]:
    if not isinstance(value, list):
        raise ScreeningDocumentError(path, "must be a list")
    groups: list[PhraseGroup] = []
    names: set[str] = set()
    for index, raw in enumerate(cast("list[object]", value)):
        where = f"{path}[{index}]"
        entry = _object(raw, where, _GROUP_KEYS)
        name = entry["name"]
        if not isinstance(name, str) or not name.strip():
            raise ScreeningDocumentError(f"{where}.name", "must be a non-empty string")
        if name.strip() in names:
            raise ScreeningDocumentError(f"{where}.name", f"repeats {name.strip()!r}")
        names.add(name.strip())
        fields = _strings(entry["fields"], f"{where}.fields", non_empty=True)
        for position, field_name in enumerate(fields):
            if field_name not in FIELDS:
                raise ScreeningDocumentError(
                    f"{where}.fields[{position}]", f"must be one of {', '.join(FIELDS)}"
                )
        if len(set(fields)) != len(fields):
            raise ScreeningDocumentError(f"{where}.fields", "repeats a field")
        groups.append(
            PhraseGroup(
                name=name.strip(),
                phrases=_strings(entry["phrases"], f"{where}.phrases", non_empty=True),
                fields=fields,
                points=_integer(entry["points"], f"{where}.points", positive=True),
            )
        )
    return tuple(groups)


def parse_screening(raw: object) -> ScreeningDocument:
    """The document, or `ScreeningDocumentError` naming the first bad field.

    Every key is required and an unknown key is refused, so a misspelt weight
    is an error rather than a default nobody chose.
    """
    top = _object(raw, "", _TOP_KEYS)
    targets = _object(top["targets"], "targets", _TARGET_KEYS)
    scoring_raw = _object(top["scoring"], "scoring", _SCORING_KEYS)
    ownership = _object(top["ownership"], "ownership", _OWNERSHIP_KEYS)
    document = ScreeningDocument(
        titles=_strings(targets["titles"], "targets.titles"),
        include=_strings(targets["title_keywords_include"], "targets.title_keywords_include"),
        exclude=_strings(targets["title_keywords_exclude"], "targets.title_keywords_exclude"),
        scoring={key: _integer(scoring_raw[key], f"scoring.{key}") for key in _SCORING_KEYS},
        bonuses=_groups(top["bonuses"], "bonuses"),
        penalties=_groups(top["penalties"], "penalties"),
        broad_title_signals=_strings(
            ownership["broad_title_signals"], "ownership.broad_title_signals"
        ),
        broad_description_signals=_strings(
            ownership["broad_description_signals"], "ownership.broad_description_signals"
        ),
        fingerprint=cast("dict[str, Any]", raw),
    )
    return document


def parse_screening_text(text: str) -> ScreeningDocument:
    try:
        raw: object = json.loads(text)
    except json.JSONDecodeError as error:
        raise ScreeningDocumentError("(file)", f"is not valid JSON: {error}") from error
    return parse_screening(raw)


def load_screening(conn: Connection, scope: Scope) -> ScreeningDocument | None:
    """The scope track's screening document, or None when it has none. A
    stored document was validated when it was put, so a parse failure here is
    a store edited by hand, and it raises."""
    for name, content in owned_documents(conn, SCREENING_KIND, scope.track.id):
        if name == SCREENING_NAME:
            return parse_screening_text(content)
    return None


# --- scoring -------------------------------------------------------------------


def _field_text(job: NormalizedJob, fields: tuple[str, ...]) -> str:
    parts = {"title": job["title"], "description": job["description"]}
    return normalize(" ".join(parts[name] for name in fields))


def _fires(group: PhraseGroup, job: NormalizedJob) -> bool:
    text = _field_text(job, group.fields)
    return any(contains_word(text, normalize(phrase)) for phrase in group.phrases)


def _include_bonus(document: ScreeningDocument) -> int:
    """Every include keyword matched, held to the cap."""
    return min(
        document.scoring["include_keyword_bonus_cap"],
        len(document.include) * document.scoring["include_keyword_bonus"],
    )


def score_bounds(document: ScreeningDocument) -> tuple[int, int]:
    """The least and the most a posting without a blocker can score: every
    negative contribution and every penalty at once, floored at zero as the
    score is, and every positive contribution at once."""
    scoring = document.scoring
    contributions = [
        scoring["exact_title_bonus"],
        _include_bonus(document),
        scoring["remote_bonus"],
        scoring["preferred_region_bonus"],
        *(group.points for group in document.bonuses),
    ]
    low = (
        scoring["base_score"]
        + sum(value for value in contributions if value < 0)
        - sum(group.points for group in document.penalties)
    )
    high = scoring["base_score"] + sum(value for value in contributions if value > 0)
    return max(0, low), max(0, high)


def blocker_penalty(document: ScreeningDocument) -> int:
    """The smallest penalty that puts any blocked posting below every
    unblocked one. Derived from the document, never configured (spec 078)."""
    low, high = score_bounds(document)
    return high - low + 1


def focused_policy_version(document: ScreeningDocument) -> str:
    """A digest of the screening document and the rule tables the gates and
    blockers read. A changed weight, phrase or table changes it (spec 079)."""
    # Imported here: policy imports rules, and its rule fingerprint is the one
    # the industry version uses, so a changed table moves both.
    from harrier.screening.policy import VERSION_LENGTH, rule_fingerprint

    payload = {"focused": dict(document.fingerprint), "rules": rule_fingerprint(), "model": "none"}
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:VERSION_LENGTH]


def focused_score(job: NormalizedJob, document: ScreeningDocument) -> FocusedScore:
    """The score a posting gets on a focused track (spec 137)."""
    scoring = document.scoring
    title = normalize(job["title"])
    scoring_text = normalize(f"{job['title']} {job['location']} {job['description']}")
    score = scoring["base_score"]
    reasons = [SCORER]

    if is_target_title_variant(title, [normalize(item) for item in document.titles]):
        score += scoring["exact_title_bonus"]
    matched = [token for token in document.include if contains_word(title, normalize(token))]
    if matched:
        score += min(
            scoring["include_keyword_bonus_cap"], len(matched) * scoring["include_keyword_bonus"]
        )
    if text_matches_any_pattern(scoring_text, REMOTE_POSITIVE_PATTERNS):
        score += scoring["remote_bonus"]
    if text_matches_any_pattern(scoring_text, PREFERRED_REGION_PATTERNS):
        score += scoring["preferred_region_bonus"]

    for group in document.bonuses:
        if _fires(group, job):
            score += group.points
            reasons.append(f"bonus={group.name}")
    for group in document.penalties:
        if _fires(group, job):
            score -= group.points
            reasons.append(f"penalty={group.name}")
    score = max(0, score)

    found = blockers(job)
    if found:
        score -= blocker_penalty(document)
        reasons.extend(f'blocker={kind} "{phrase}"' for kind, phrase in found)
    return FocusedScore(score=score, reasons=reasons, version=focused_policy_version(document))


def failed_gate(
    job: NormalizedJob, document: ScreeningDocument, candidate_cfg: CandidateConfig
) -> str | None:
    """The first industry gate this posting fails, in the industry order, or
    None. The title rule reads the document's targets; the remote and region
    gate reads the shared candidate configuration's regions, as it does on
    the industry track."""
    if not title_allowed(job["title"], document.targets_config()):
        return "title"
    regions = candidate_cfg.get("candidate")
    allowed, reason = remote_region_allowed(job, {"candidate": regions if regions else {}})
    return None if allowed else reason


def put_screening(conn: Connection, scope: Scope, text: str) -> ScreeningDocument:
    """Store `text` as the scope track's screening document, as read, once it
    parses. Raises `ScreeningDocumentError` for a bad document and writes
    nothing; the store refuses a track that is not focused or is archived."""
    from harrier.profile.store import put_document

    document = parse_screening_text(text)
    put_document(conn, SCREENING_KIND, SCREENING_NAME, "json", text, track_id=scope.track.id)
    return document
