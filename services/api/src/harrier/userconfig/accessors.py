"""Reading user configuration: store first, committed file second.

Every accessor takes an optional connection. Passing None is not an error;
it means "no store available here", which is how the file-based callers and
the tests that predate spec 023 keep working unchanged.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import cast

from harrier.demo import resolve_config_path
from harrier.screening.normalized import normalize
from harrier.sources.feeds import FEEDS_PATH, read_line_config, route_ats_feeds
from harrier.userconfig.store import (
    COMPANY_HOLDS,
    DISCOVERY,
    FEEDS,
    LINKEDIN_SEARCHES,
    ConfigError,
    HoldEntry,
    get_config,
    hold_is_active,
    parse_hold_until,
    stored_list,
)

SEARCH_URLS_PATH = Path("config") / "linkedin_search_urls.txt"
DISCOVERY_PATH = Path("config") / "discovery.json"
HOLDS_PATH = Path("config") / "companies-hold.csv"


def load_feed_urls(conn: sqlite3.Connection | None = None) -> list[str]:
    stored = stored_list(conn, FEEDS)
    if stored is not None:
        return stored
    return read_line_config(resolve_config_path(FEEDS_PATH))


def load_ats_feeds(conn: sqlite3.Connection | None = None) -> dict[str, list[str]]:
    """The board watchlist grouped by importer, from wherever it lives."""
    return route_ats_feeds(load_feed_urls(conn))


def load_search_urls(conn: sqlite3.Connection | None = None) -> list[str]:
    stored = stored_list(conn, LINKEDIN_SEARCHES)
    if stored is not None:
        return stored
    return read_line_config(resolve_config_path(SEARCH_URLS_PATH))


def load_discovery_settings(conn: sqlite3.Connection | None = None) -> dict[str, object]:
    if conn is not None:
        value = get_config(conn, DISCOVERY)
        if value is not None:
            if not isinstance(value, dict):
                raise ConfigError("stored discovery configuration is not an object")
            return cast("dict[str, object]", value)
    path = resolve_config_path(DISCOVERY_PATH)
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return cast("dict[str, object]", parsed) if isinstance(parsed, dict) else {}


def load_hold_companies(
    conn: sqlite3.Connection | None = None, today: date | None = None
) -> set[str]:
    """Normalized names of the companies on an active hold.

    A hold with no date is active indefinitely; a dated hold is active on and
    before its date and lapses the day after, with nothing written (spec 052).
    Expiry is decided here, on every read, so the store and the CSV fallback
    apply the same rule. A company named by several holds is held while any
    of them is active.

    The stored form drops the reason column the CSV carries: the reason is
    personal operational commentary and nothing reads it (ADR-008).
    """
    today = today or date.today()
    if conn is not None:
        stored = get_config(conn, COMPANY_HOLDS)
        if stored is not None:
            return _active_names(cast("list[HoldEntry]", stored), today)
    return read_hold_file(resolve_config_path(HOLDS_PATH), today)


def _active_names(entries: list[HoldEntry], today: date) -> set[str]:
    names: set[str] = set()
    for entry in entries:
        if isinstance(entry, str):
            company, until = entry, None
        else:
            company = entry["company"]
            until = parse_hold_until(entry.get("hold_until", ""), company)
        name = normalize(company)
        if name and hold_is_active(until, today):
            names.add(name)
    return names


def read_hold_file(path: Path, today: date | None = None) -> set[str]:
    """Active holds from the CSV fallback. A malformed date raises, naming
    the file and the company, so discovery fails loudly rather than
    screening against the wrong holds."""
    return _active_names(read_hold_file_raw(path), today or date.today())


def read_hold_file_raw(path: Path) -> list[HoldEntry]:
    """Holds as written, for the import path. A row with a date becomes the
    object form so the date survives the import; a row without one stays a
    bare name, the shape every hold had before spec 052. Expired rows are
    kept: expiry is applied when holds are read, not when they are imported.
    Normalization happens at read time so the stored value stays legible to
    whoever edits it."""
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    entries: list[HoldEntry] = []
    for row in rows:
        company = str(row.get("company", "") or "").strip()
        if not company:
            continue
        until = str(row.get("hold_until", "") or "").strip()
        try:
            parse_hold_until(until, company)
        except ConfigError as error:
            raise ConfigError(f"{path}: {error}") from error
        entries.append({"company": company, "hold_until": until} if until else company)
    return entries
