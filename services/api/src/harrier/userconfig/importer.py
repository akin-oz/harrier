"""`harrier config import`, as one function both surfaces call (specs 023, 096).

It lived inline in the CLI's dispatch, and the browser needed the same
import. Two copies of it would be two answers to which files count and how a
malformed hold date is refused, so the CLI and `POST /config/import` both
reach `import_config_files` and print or return what it reports.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from harrier.sources.feeds import FEEDS_PATH, read_line_config
from harrier.userconfig.accessors import (
    DISCOVERY_PATH,
    HOLDS_PATH,
    SEARCH_URLS_PATH,
    read_hold_file_raw,
)
from harrier.userconfig.store import (
    COMPANY_HOLDS,
    DISCOVERY,
    FEEDS,
    KINDS,
    LINKEDIN_SEARCHES,
    HoldEntry,
    set_config,
)

ENTRIES = "entries"
SETTINGS = "settings"


@dataclass(frozen=True)
class ImportedKind:
    kind: str
    count: int
    unit: str

    def line(self) -> str:
        return f"{self.kind}: {self.count} {self.unit} imported"


@dataclass(frozen=True)
class ConfigImport:
    """What one import stored, which files it found nothing in, and the
    command's report of both in the order it reached them."""

    imported: list[ImportedKind] = field(default_factory=list[ImportedKind])
    skipped: list[str] = field(default_factory=list[str])
    report: list[str] = field(default_factory=list[str])

    @property
    def total(self) -> int:
        return len(KINDS)


def settings_from_file(path: Path) -> dict[str, object]:
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    # The committed example carries a _comment key for the reader; it is not
    # a setting and must not become one.
    return {
        key: value
        for key, value in cast("dict[str, object]", parsed).items()
        if not key.startswith("_")
    }


def import_config_files(conn: sqlite3.Connection) -> ConfigImport:
    """Read each committed or local file once into the store.

    Every line file is read before anything is stored, so a malformed hold
    date refuses the whole import rather than half of it (spec 052). Raises
    `ConfigError` for that, and for any value the store refuses.
    """
    sources: dict[str, list[str] | list[HoldEntry]] = {
        FEEDS: read_line_config(FEEDS_PATH),
        LINKEDIN_SEARCHES: read_line_config(SEARCH_URLS_PATH),
        COMPANY_HOLDS: read_hold_file_raw(HOLDS_PATH),
    }
    result = ConfigImport()
    for kind, values in sources.items():
        if not values:
            result.skipped.append(kind)
            result.report.append(f"{kind}: no file to import, skipped")
            continue
        set_config(conn, kind, values)
        entry = ImportedKind(kind=kind, count=len(values), unit=ENTRIES)
        result.imported.append(entry)
        result.report.append(entry.line())
    settings = settings_from_file(DISCOVERY_PATH)
    if settings:
        set_config(conn, DISCOVERY, settings)
        entry = ImportedKind(kind=DISCOVERY, count=len(settings), unit=SETTINGS)
        result.imported.append(entry)
        result.report.append(entry.line())
    return result
