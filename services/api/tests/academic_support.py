"""Shared synthetic inputs for the academic discovery tests (spec 097).

The search entry is the committed example's, and the dataset is the
committed synthetic fixture: invented organisations at reserved hosts,
written by hand (ADR-008, `fixtures/PROVENANCE.md`).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
ACADEMIC_FIXTURE = REPO_ROOT / "fixtures" / "academic-dataset.json"
EXAMPLE_SEARCHES = REPO_ROOT / "config" / "academic-searches.example.json"


def academic_search_entry(**overrides: Any) -> dict[str, Any]:
    """The example's one entry, with top-level fields replaced."""
    examples: dict[str, Any] = json.loads(EXAMPLE_SEARCHES.read_text(encoding="utf-8"))
    entry: dict[str, Any] = copy.deepcopy(next(iter(examples.values())))
    entry.update(overrides)
    return entry


def fixture_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = json.loads(ACADEMIC_FIXTURE.read_text(encoding="utf-8"))
    return items
