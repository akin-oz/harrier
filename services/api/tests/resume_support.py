"""The committed synthetic resume content, for tests (spec 098).

The examples are two documents, the facts and the industry framing. Tests
that build a bundle by hand still write one object in the old shape and
store it through `store_resume`, which splits it the way
`harrier profile split-resume` does.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, cast

from harrier.profile.store import put_document
from harrier.resume.documents import (
    FACTS_KIND,
    FACTS_NAME,
    FRAMING_KIND,
    FRAMING_NAME,
    merge_documents,
    split_document,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
FACTS_EXAMPLE = REPO_ROOT / "config" / "resume-facts.example.json"
FRAMING_EXAMPLE = REPO_ROOT / "config" / "resume-framing.example.json"


def _read(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))


def example_documents() -> tuple[dict[str, Any], dict[str, Any]]:
    return _read(FACTS_EXAMPLE), _read(FRAMING_EXAMPLE)


def example_bundle_raw() -> dict[str, Any]:
    """The two examples as one object, the shape `parse_bundle` reads."""
    result = merge_documents(*example_documents())
    assert not result.errors, result.errors
    return cast("dict[str, Any]", result.merged)


def store_documents(
    conn: sqlite3.Connection, facts: dict[str, object], framing: dict[str, object]
) -> None:
    put_document(conn, FACTS_KIND, FACTS_NAME, "json", json.dumps(facts))
    put_document(conn, FRAMING_KIND, FRAMING_NAME, "json", json.dumps(framing))


def store_resume(conn: sqlite3.Connection, raw: dict[str, object]) -> None:
    """Store one old-shape object as the two documents."""
    facts, framing, unplaced = split_document(raw)
    assert not unplaced, unplaced
    store_documents(conn, facts, framing)


def store_example(conn: sqlite3.Connection) -> None:
    store_documents(conn, *example_documents())
