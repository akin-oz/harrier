"""Per-source seen-state: the cross-run dedupe layer (spec 007 port)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from harrier.db import data_dir

SEEN_CAP = 10_000


def _state_path(source_name: str) -> Path:
    return data_dir() / "discovery" / f"{source_name}_seen.json"


def load_seen_keys(source_name: str) -> set[str]:
    path = _state_path(source_name)
    if not path.is_file():
        return set()
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(parsed, dict):
        return set()
    # JSON object keys are always strings; the cast states that.
    record = cast("dict[str, object]", parsed)
    raw_keys = record.get("seen_keys")
    if not isinstance(raw_keys, list):
        return set()
    return {str(key) for key in cast("list[object]", raw_keys)}


def save_seen_keys(source_name: str, seen_keys: set[str]) -> None:
    """Persist the seen set, capped at SEEN_CAP keys.

    The old code sliced list(set)[-10000:], an arbitrary subset; sorting
    first makes the retained subset deterministic without changing the
    contract (membership testing across runs).
    """
    path = _state_path(source_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "seen_keys": sorted(seen_keys)[-SEEN_CAP:],
        "updated_at": datetime.now(UTC).isoformat(),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
