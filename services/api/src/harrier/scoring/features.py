"""What the learned score reads from a posting (spec 077).

Curated, named and few. Each feature comes from title, location and
description, and each reuses a table `harrier.screening.rules` already owns,
so the rule score and the learned score cannot disagree about what a phrase
means.

The blockers are not features (spec 081). A posting the candidate cannot
take is rare among the decisions the model learns from, so a weight for it
would be noise; instead `harrier.scoring.model` floors a blocked posting
below every eligible one, by the same derivation spec 078 uses for the rule
score, and `rules.blockers` stays the one definition of a blocker.

Deterministic by construction: no randomness, no clock, no network, and the
same job always yields the same vector, which is what lets the export and
inference share this one extractor.

There is deliberately no source feature (the ingestion-only invariant: no
per-source scoring) and no length feature (rewarding length is the defect
the rule score has).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, cast

from harrier.screening.descriptions import MIN_DESCRIPTION_LENGTH_FOR_SCORING
from harrier.screening.normalized import NormalizedJob, normalize
from harrier.screening.rules import (
    BACKEND_TERMS,
    FRONTEND_TERMS,
    CandidateConfig,
    contains_word,
    is_target_title_variant,
    location_names_explicit_emea,
    scoring_config,
)

# The order is part of the model file. A model trained against a different
# order is refused rather than read with its coefficients misaligned.
FEATURE_ORDER: tuple[str, ...] = (
    "skill_signal",
    "preferred_signal",
    "title_fit",
    "frontend_share",
    "explicit_emea_remote",
    "years_gap",
)

# Unbounded counts, divided by their 92nd percentile over the training rows
# and clipped to 1 at inference. The rest are already between 0 and 1.
NUMERIC_FEATURES: frozenset[str] = frozenset({"skill_signal", "preferred_signal", "years_gap"})

# The fallback a job takes when it cannot be judged at all.
DESCRIPTION_MISSING = "description-missing"

# A stated requirement: "5+ years of experience", "3-5 years of professional
# experience", "at least 4 yrs experience". The lower bound of a range is the
# requirement. A number in another sentence ("founded 10 years ago") is not.
_REQUIRED_YEARS = re.compile(
    r"\b(\d{1,2})\s*\+?\s*(?:(?:-|to)\s*\d{1,2}\s*\+?\s*)?(?:years?|yrs?)\b"
    r"(?:\s+of)?(?:\s+[a-z/-]+){0,3}?\s+experience\b"
)
_PLAUSIBLE_YEARS = range(1, 31)


@dataclass(frozen=True)
class Extraction:
    """A job as the model reads it.

    `values` are raw, before normalization, keyed by `FEATURE_ORDER`.
    `notes` are signals the row should carry even though no feature names
    them, such as a configuration key the extractor needed and did not find.
    """

    values: dict[str, float]
    notes: tuple[str, ...]

    def vector(self) -> list[float]:
        return [self.values[name] for name in FEATURE_ORDER]


def validate(job: NormalizedJob) -> str | None:
    """Whether the model can judge this job at all; the fallback when not.

    Every feature but the title's reads the description, so without one the
    model would score a posting on its title and call the result learned.
    """
    if len(job["description"].strip()) < MIN_DESCRIPTION_LENGTH_FOR_SCORING:
        return DESCRIPTION_MISSING
    return None


def _candidate_years(candidate_cfg: CandidateConfig) -> float | None:
    raw = candidate_cfg.get("candidate")
    section = cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
    value = section.get("years_experience")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def required_years(text: str) -> int | None:
    """The largest stated experience requirement in normalized text, if any."""
    found = [int(match.group(1)) for match in _REQUIRED_YEARS.finditer(text)]
    plausible = [years for years in found if years in _PLAUSIBLE_YEARS]
    return max(plausible) if plausible else None


def extract(job: NormalizedJob, candidate_cfg: CandidateConfig) -> Extraction:
    """The feature vector for one job, with any notes the row should carry."""
    title = normalize(job["title"])
    text = normalize(f"{job['title']} {job['description']}")
    scoring = scoring_config(candidate_cfg)
    values: dict[str, float] = {}
    notes: list[str] = []

    skill_weights = cast("dict[str, int]", scoring["skill_signals"])
    values["skill_signal"] = float(
        sum(weight for token, weight in skill_weights.items() if contains_word(text, token))
    )
    preferred_weights = cast("dict[str, int]", scoring["preferred_signal_weights"])
    values["preferred_signal"] = float(
        sum(weight for token, weight in preferred_weights.items() if contains_word(text, token))
    )

    raw_targets = candidate_cfg.get("targets")
    targets = cast("dict[str, Any]", raw_targets) if isinstance(raw_targets, dict) else {}
    exact_titles = [
        normalize(str(item)) for item in cast("list[object]", targets.get("titles") or [])
    ]
    include = [
        normalize(str(item))
        for item in cast("list[object]", targets.get("title_keywords_include") or [])
    ]
    if is_target_title_variant(title, exact_titles):
        values["title_fit"] = 1.0
    else:
        matched = sum(1 for token in include if token and contains_word(title, token))
        cap = int(scoring["include_keyword_bonus_cap"])
        bonus = int(scoring["include_keyword_bonus"])
        values["title_fit"] = min(1.0, matched * bonus / cap) if cap > 0 else 0.0

    frontend = sum(1 for term in FRONTEND_TERMS if contains_word(text, term))
    backend = sum(1 for term in BACKEND_TERMS if contains_word(text, term))
    values["frontend_share"] = frontend / (frontend + backend) if frontend + backend else 0.5

    values["explicit_emea_remote"] = 1.0 if location_names_explicit_emea(job["location"]) else 0.0

    candidate_years = _candidate_years(candidate_cfg)
    required = required_years(normalize(job["description"]))
    if candidate_years is None:
        values["years_gap"] = 0.0
        notes.append("years_experience unset")
    else:
        values["years_gap"] = max(0.0, float(required) - candidate_years) if required else 0.0

    return Extraction(values=values, notes=tuple(notes))
