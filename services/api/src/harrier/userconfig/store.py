"""User configuration in the database (spec 023, ADR-009).

The board watchlist, the LinkedIn searches, the discovery settings, and the
hold list were gitignored loose files. They are user data, so they belong
where user data lives (ADR-008), and putting them there is what makes the
repo customizable without editing a checkout: the same values become
editable through the API and, later, the GUI.

Each kind is one row holding a JSON value. That mirrors how the files read
(a list of lines, a settings object, a list of company names) and keeps the
accessors list-shaped, rather than inventing a row-per-item schema that
nothing yet needs.

Resolution order for every accessor:

1. the store, when a row exists
2. the committed or local file, which is how an existing install keeps
   working before `harrier config import` runs, and how demo mode gets its
   synthetic values (harrier.demo.resolve_config_path)
3. empty

Step 2 is what lets this ship without a migration being mandatory. A fresh
clone with no files and no rows runs cleanly with no sources, which is the
spec's acceptance criterion, not an error.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from typing import cast

FEEDS = "feeds"
LINKEDIN_SEARCHES = "linkedin_searches"
DISCOVERY = "discovery"
COMPANY_HOLDS = "company_holds"
# One object keyed by track slug, each value that track's search entry
# (spec 097). Store only: no file fallback, because a loose file would be a
# second home for the search (ADR-009, decision 2).
ACADEMIC_SEARCHES = "academic_searches"

KINDS = (FEEDS, LINKEDIN_SEARCHES, DISCOVERY, COMPANY_HOLDS, ACADEMIC_SEARCHES)


class ConfigError(ValueError):
    """A configuration value is not the shape its kind requires."""


# A hold is a bare company name, or an object naming the company and,
# optionally, the last day the hold applies (spec 052). The bare form is the
# shape every hold had before expiry existed, so it stays valid as-is.
HoldEntry = str | dict[str, str]

HOLD_KEYS = frozenset({"company", "hold_until"})

# `date.fromisoformat` accepts more than the CSV column's format (`20260630`,
# for one), so the shape is checked first and the calendar second.
_HOLD_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_hold_until(value: str, company: str) -> date | None:
    """The last day a hold applies, or None for a hold with no expiry.

    Empty means no expiry, which is how the CSV column reads. Anything else
    must be a real `YYYY-MM-DD` date: a malformed one is refused rather than
    read as "no expiry", because that fallback is the permanent-hold bug this
    spec exists to remove, arriving by a different route.
    """
    text = value.strip()
    if not text:
        return None
    if _HOLD_DATE.fullmatch(text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            pass
    raise ConfigError(
        f"hold for {company!r} has a malformed hold_until {value!r}; expected YYYY-MM-DD"
    )


def hold_is_active(until: date | None, today: date) -> bool:
    """Active with no date, or on and before its date (inclusive)."""
    return until is None or today <= until


def _validate_holds(items: list[object]) -> list[HoldEntry]:
    entries: list[HoldEntry] = []
    for item in items:
        if isinstance(item, str):
            if item.strip():
                entries.append(item.strip())
            continue
        if not isinstance(item, dict):
            raise ConfigError(
                f"{COMPANY_HOLDS} entries must be strings or objects, got {type(item).__name__}"
            )
        entry = cast("dict[object, object]", item)
        unknown = sorted(str(key) for key in entry if key not in HOLD_KEYS)
        if unknown:
            # Most likely a typo of hold_until. Accepting it would store a
            # hold that never expires while looking as though it does.
            raise ConfigError(
                f"{COMPANY_HOLDS} entry {entry!r} has unknown keys {unknown}; "
                f"expected only {sorted(HOLD_KEYS)}"
            )
        company = entry.get("company")
        if not isinstance(company, str) or not company.strip():
            raise ConfigError(f"{COMPANY_HOLDS} entry {entry!r} needs a non-blank company")
        normalized: dict[str, str] = {"company": company.strip()}
        if "hold_until" in entry:
            until = entry["hold_until"]
            if not isinstance(until, str):
                raise ConfigError(
                    f"hold for {company.strip()!r} has a hold_until that is not a string"
                )
            if parse_hold_until(until, company.strip()) is not None:
                normalized["hold_until"] = until.strip()
        entries.append(normalized)
    return entries


def _validate(kind: str, value: object, *, on_read: bool = False) -> object:
    """Reject a value that its readers would later mishandle, and normalize
    what survives. Used on both the write and the read path: a bad value
    stored once would otherwise surface as a confusing failure inside
    discovery, far from whoever set it, and a row can appear without going
    through set_config at all.

    Normalization (trimming, dropping blanks) is idempotent, so applying it
    twice on a value that was written through set_config changes nothing.
    """
    if kind not in KINDS:
        raise ConfigError(f"unknown configuration kind {kind!r}; expected one of {KINDS}")
    if kind == ACADEMIC_SEARCHES:
        # Imported here: the search module reads the source's constants and
        # the slug rule, and this store sits below both.
        from harrier.academic.search import SearchError, validate_searches

        # A write enforces the hard limits on the ceilings; a read checks the
        # shape, and the ceilings are clamped where the entry is used
        # (spec 035's rule, spec 097).
        try:
            return validate_searches(value, enforce_limits=not on_read)
        except SearchError as exc:
            raise ConfigError(str(exc)) from exc
    if kind == DISCOVERY:
        if not isinstance(value, dict):
            raise ConfigError(f"{kind} must be a JSON object, got {type(value).__name__}")
        return cast("dict[str, object]", value)
    if not isinstance(value, list):
        raise ConfigError(f"{kind} must be a JSON list, got {type(value).__name__}")
    items = cast("list[object]", value)
    if kind == COMPANY_HOLDS:
        return _validate_holds(items)
    for item in items:
        if not isinstance(item, str):
            raise ConfigError(f"{kind} entries must be strings, got {type(item).__name__}")
    return [item.strip() for item in cast("list[str]", items) if item.strip()]


def set_config(conn: sqlite3.Connection, kind: str, value: object) -> None:
    stored = _validate(kind, value)
    with conn:
        conn.execute(
            """
            INSERT INTO user_config (kind, value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT (kind) DO UPDATE SET
                value = excluded.value,
                updated_at = datetime('now')
            """,
            (kind, json.dumps(stored, ensure_ascii=False)),
        )


def get_config(conn: sqlite3.Connection, kind: str) -> object | None:
    """The stored value, or None when there is no row for the kind.

    None and an empty list are different answers: no row means fall back to
    the file, an empty list means the user cleared the watchlist on purpose.
    """
    row = conn.execute("SELECT value FROM user_config WHERE kind = ?", (kind,)).fetchone()
    if row is None:
        return None
    try:
        parsed: object = json.loads(str(row[0]))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"stored {kind} configuration is not valid JSON: {exc}") from exc
    # Validated on the way out as well as the way in. Writing through
    # set_config is not the only way a row can appear: a hand-edited
    # database, a restored backup, or a future migration can all put a bad
    # value here, and the read path was coercing rather than refusing, so
    # a stored [7] reached discovery as ["7"] (review finding on PR #20).
    return _validate(kind, parsed, on_read=True)


def delete_config(conn: sqlite3.Connection, kind: str) -> bool:
    with conn:
        cursor = conn.execute("DELETE FROM user_config WHERE kind = ?", (kind,))
    return cursor.rowcount > 0


def list_config(conn: sqlite3.Connection) -> list[dict[str, str]]:
    columns = ("kind", "value", "updated_at")
    rows = conn.execute(f"SELECT {', '.join(columns)} FROM user_config ORDER BY kind").fetchall()
    return [dict(zip(columns, (str(value) for value in row), strict=True)) for row in rows]


def stored_list(conn: sqlite3.Connection | None, kind: str) -> list[str] | None:
    """A stored list of strings, or None to mean "fall back to the file".

    For the line-list kinds only. A hold list may carry objects (spec 052)
    and is read by `load_hold_companies` instead.
    """
    if conn is None:
        return None
    value = get_config(conn, kind)
    if value is None:
        return None
    # get_config validates, so a list of strings is the only thing that can
    # arrive here: nothing to re-check and nothing to coerce.
    return cast("list[str]", value)
