"""Offer evaluation and batch prospect evaluation (spec 015).

The six-block report with the machine verdict contract, seed stories and
the bounded story bank from the profile store, and the batch driver with
opt-in, audit-logged auto-reject.
"""

from __future__ import annotations

from harrier.offers.batch import BatchOptions, BatchSummary, evaluate_prospects
from harrier.offers.evaluate import (
    VERDICTS,
    EvaluationError,
    EvaluationResult,
    Verdict,
    build_system_prompt,
    evaluate_offer,
    parse_json_response,
    parse_verdict,
    report_path_for,
)
from harrier.offers.report import ARCHETYPES, VERDICT_BADGES, build_report
from harrier.offers.stories import capture_stories, load_seed_stories, load_story_bank

__all__ = [
    "ARCHETYPES",
    "VERDICTS",
    "VERDICT_BADGES",
    "BatchOptions",
    "BatchSummary",
    "EvaluationError",
    "EvaluationResult",
    "Verdict",
    "build_report",
    "build_system_prompt",
    "capture_stories",
    "evaluate_offer",
    "evaluate_prospects",
    "load_seed_stories",
    "load_story_bank",
    "parse_json_response",
    "parse_verdict",
    "report_path_for",
]
