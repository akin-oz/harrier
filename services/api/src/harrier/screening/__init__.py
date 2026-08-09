"""Screening: the one shared path for filtering, scoring, and dedupe.

Every job source is ingestion only: sources produce NormalizedJob values and
nothing else. Screening happens here, once (product invariant; spec 007).
"""

from harrier.screening.archetypes import detect_archetype
from harrier.screening.normalized import (
    NormalizedJob,
    dedupe_normalized_jobs,
    make_normalized_job,
    normalize,
    stable_key,
)
from harrier.screening.pipeline import (
    ScreenResult,
    TrackerIndexes,
    build_tracker_indexes,
    build_tracker_row,
    screen_jobs,
)
from harrier.screening.rules import (
    SCORE_CUTOFF,
    CandidateConfig,
    remote_region_allowed,
    score_job,
    scoring_config,
    title_allowed,
)

__all__ = [
    "SCORE_CUTOFF",
    "CandidateConfig",
    "NormalizedJob",
    "ScreenResult",
    "TrackerIndexes",
    "build_tracker_indexes",
    "build_tracker_row",
    "dedupe_normalized_jobs",
    "detect_archetype",
    "make_normalized_job",
    "normalize",
    "remote_region_allowed",
    "score_job",
    "scoring_config",
    "screen_jobs",
    "stable_key",
    "title_allowed",
]
