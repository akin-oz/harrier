"""Reading user configuration: store first, committed file second.

Every accessor takes an optional connection. Passing None is not an error;
it means "no store available here", which is how the file-based callers and
the tests that predate spec 023 keep working unchanged.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import cast

from harrier.demo import resolve_config_path
from harrier.screening.normalized import normalize
from harrier.sources.feeds import FEEDS_PATH, read_line_config, route_ats_feeds
from harrier.userconfig.store import (
    COMPANY_HOLDS,
    DEFAULT_SCOPE,
    DISCOVERY,
    FEEDS,
    LINKEDIN_SEARCHES,
    ConfigError,
    get_config,
    stored_list,
)

SEARCH_URLS_PATH = Path("config") / "linkedin_search_urls.txt"
DISCOVERY_PATH = Path("config") / "discovery.json"
HOLDS_PATH = Path("config") / "companies-hold.csv"


def load_feed_urls(
    conn: sqlite3.Connection | None = None, *, scope: str = DEFAULT_SCOPE
) -> list[str]:
    stored = stored_list(conn, FEEDS, scope=scope)
    if stored is not None:
        return stored
    return read_line_config(resolve_config_path(FEEDS_PATH))


def load_ats_feeds(
    conn: sqlite3.Connection | None = None, *, scope: str = DEFAULT_SCOPE
) -> dict[str, list[str]]:
    """The board watchlist grouped by importer, from wherever it lives."""
    return route_ats_feeds(load_feed_urls(conn, scope=scope))


def load_search_urls(
    conn: sqlite3.Connection | None = None, *, scope: str = DEFAULT_SCOPE
) -> list[str]:
    stored = stored_list(conn, LINKEDIN_SEARCHES, scope=scope)
    if stored is not None:
        return stored
    return read_line_config(resolve_config_path(SEARCH_URLS_PATH))


def load_discovery_settings(
    conn: sqlite3.Connection | None = None, *, scope: str = DEFAULT_SCOPE
) -> dict[str, object]:
    if conn is not None:
        value = get_config(conn, DISCOVERY, scope=scope)
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
    conn: sqlite3.Connection | None = None, *, scope: str = DEFAULT_SCOPE
) -> set[str]:
    """Normalized company names on hold. The stored form drops the reason
    column the CSV carries: the reason is personal operational commentary
    and nothing reads it (ADR-008)."""
    stored = stored_list(conn, COMPANY_HOLDS, scope=scope)
    if stored is not None:
        return {name for name in (normalize(item) for item in stored) if name}
    return read_hold_file(resolve_config_path(HOLDS_PATH))


def read_hold_file(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    companies: set[str] = set()
    for row in rows:
        company = normalize(row.get("company", "") or "")
        if company:
            companies.add(company)
    return companies


def read_hold_file_raw(path: Path) -> list[str]:
    """Company names as written, for the import path. Normalization happens
    at read time so the stored value stays legible to whoever edits it."""
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [str(row.get("company", "") or "").strip() for row in rows if row.get("company")]
