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

1. the store, when a row exists for the scope
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
import sqlite3
from typing import cast

DEFAULT_SCOPE = "default"

FEEDS = "feeds"
LINKEDIN_SEARCHES = "linkedin_searches"
DISCOVERY = "discovery"
COMPANY_HOLDS = "company_holds"

KINDS = (FEEDS, LINKEDIN_SEARCHES, DISCOVERY, COMPANY_HOLDS)


class ConfigError(ValueError):
    """A configuration value is not the shape its kind requires."""


def _validate(kind: str, value: object) -> object:
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
    if kind == DISCOVERY:
        if not isinstance(value, dict):
            raise ConfigError(f"{kind} must be a JSON object, got {type(value).__name__}")
        return cast("dict[str, object]", value)
    if not isinstance(value, list):
        raise ConfigError(f"{kind} must be a JSON list, got {type(value).__name__}")
    items = cast("list[object]", value)
    for item in items:
        if not isinstance(item, str):
            raise ConfigError(f"{kind} entries must be strings, got {type(item).__name__}")
    return [item.strip() for item in cast("list[str]", items) if item.strip()]


def set_config(
    conn: sqlite3.Connection, kind: str, value: object, *, scope: str = DEFAULT_SCOPE
) -> None:
    stored = _validate(kind, value)
    with conn:
        conn.execute(
            """
            INSERT INTO user_config (scope, kind, value, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT (scope, kind) DO UPDATE SET
                value = excluded.value,
                updated_at = datetime('now')
            """,
            (scope, kind, json.dumps(stored, ensure_ascii=False)),
        )


def get_config(conn: sqlite3.Connection, kind: str, *, scope: str = DEFAULT_SCOPE) -> object | None:
    """The stored value, or None when this scope has no row for the kind.

    None and an empty list are different answers: no row means fall back to
    the file, an empty list means the user cleared the watchlist on purpose.
    """
    row = conn.execute(
        "SELECT value FROM user_config WHERE scope = ? AND kind = ?", (scope, kind)
    ).fetchone()
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
    return _validate(kind, parsed)


def delete_config(conn: sqlite3.Connection, kind: str, *, scope: str = DEFAULT_SCOPE) -> bool:
    with conn:
        cursor = conn.execute("DELETE FROM user_config WHERE scope = ? AND kind = ?", (scope, kind))
    return cursor.rowcount > 0


def list_config(conn: sqlite3.Connection, *, scope: str = DEFAULT_SCOPE) -> list[dict[str, str]]:
    columns = ("kind", "value", "updated_at")
    rows = conn.execute(
        f"SELECT {', '.join(columns)} FROM user_config WHERE scope = ? ORDER BY kind", (scope,)
    ).fetchall()
    return [dict(zip(columns, (str(value) for value in row), strict=True)) for row in rows]


def stored_list(
    conn: sqlite3.Connection | None, kind: str, *, scope: str = DEFAULT_SCOPE
) -> list[str] | None:
    """A stored list value, or None to mean "fall back to the file"."""
    if conn is None:
        return None
    value = get_config(conn, kind, scope=scope)
    if value is None:
        return None
    # get_config validates, so a list of strings is the only thing that can
    # arrive here: nothing to re-check and nothing to coerce.
    return cast("list[str]", value)
