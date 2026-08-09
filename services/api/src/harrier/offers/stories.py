"""Seed stories and the bounded story bank (spec 015).

Stated change from the old code: seeds live in the profile store (kind
story_seeds) instead of code constants, and captured stories deduplicate
by story_id into a bounded json document (kind story_bank) instead of an
append-only markdown file.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import cast

from harrier.profile.store import get_document, put_document

STORY_SEEDS_KIND = "story_seeds"
STORY_SEEDS_NAME = "story-seeds.json"
STORY_BANK_KIND = "story_bank"
STORY_BANK_NAME = "story-bank.json"
STORY_BANK_LIMIT = 200


def load_seed_stories(conn: sqlite3.Connection) -> list[dict[str, object]]:
    content = get_document(conn, STORY_SEEDS_KIND, STORY_SEEDS_NAME)
    if content is None:
        return []
    try:
        parsed: object = json.loads(content)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [
        cast("dict[str, object]", item)
        for item in cast("list[object]", parsed)
        if isinstance(item, dict)
    ]


def load_story_bank(conn: sqlite3.Connection) -> list[dict[str, object]]:
    content = get_document(conn, STORY_BANK_KIND, STORY_BANK_NAME)
    if content is None:
        return []
    try:
        parsed: object = json.loads(content)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [
        cast("dict[str, object]", item)
        for item in cast("list[object]", parsed)
        if isinstance(item, dict)
    ]


def capture_stories(
    conn: sqlite3.Connection,
    stories: list[dict[str, object]],
    company: str,
    role: str,
) -> int:
    """Merge captured stories into the bank: dedupe by story_id (newest
    wins), keep the bound. Returns how many entries the bank holds."""
    bank = load_story_bank(conn)
    by_id: dict[str, dict[str, object]] = {}
    for entry in bank:
        story_id = str(entry.get("story_id", ""))
        if story_id:
            by_id[story_id] = entry
    captured_at = datetime.now(UTC).isoformat()
    for story in stories:
        story_id = str(story.get("story_id", ""))
        if not story_id:
            continue
        by_id[story_id] = {
            **story,
            "company": company,
            "role": role,
            "captured_at": captured_at,
        }
    # Newest last; drop the oldest beyond the bound.
    entries = sorted(by_id.values(), key=lambda item: str(item.get("captured_at", "")))
    entries = entries[-STORY_BANK_LIMIT:]
    put_document(
        conn,
        STORY_BANK_KIND,
        STORY_BANK_NAME,
        "json",
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
    )
    return len(entries)
