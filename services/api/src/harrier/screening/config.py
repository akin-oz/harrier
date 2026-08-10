"""Candidate config and hold list loading (spec 007).

The real candidate config is personal and lives in the profile store
(ADR-008, imported by spec 004). The committed example carries structure and
default weights only, and doubles as the demo-mode config. The real hold
list is never-in-git (its reason column is personal operational data); the
committed example documents the shape.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import cast

from harrier.demo import anchored_path
from harrier.profile import get_document
from harrier.screening.normalized import normalize
from harrier.screening.rules import CandidateConfig

EXAMPLE_CONFIG_PATH = Path("config") / "candidate.example.json"
HOLDS_PATH = Path("config") / "companies-hold.csv"


def load_candidate_config(
    conn: sqlite3.Connection | None = None,
    *,
    example_path: Path | None = None,
) -> CandidateConfig:
    """Profile store first (kind=candidate), committed example as fallback."""
    if conn is not None:
        stored = get_document(conn, "candidate", "candidate.json")
        if stored is not None:
            parsed: object = json.loads(stored)
            if isinstance(parsed, dict):
                return cast(CandidateConfig, parsed)
    path = anchored_path(example_path if example_path is not None else EXAMPLE_CONFIG_PATH)
    parsed_example: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed_example, dict):
        raise ValueError(f"candidate config at {path} is not a JSON object")
    return cast(CandidateConfig, parsed_example)


def load_hold_companies(path: Path | None = None) -> set[str]:
    """Normalized company names from the hold CSV; missing file means none."""
    holds_path = path if path is not None else HOLDS_PATH
    if not holds_path.is_file():
        return set()
    with holds_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    companies: set[str] = set()
    for row in rows:
        company = normalize(row.get("company", "") or "")
        if company:
            companies.add(company)
    return companies
