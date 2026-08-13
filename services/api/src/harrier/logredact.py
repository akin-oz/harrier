"""Identity redaction for the log stream (spec 045).

`docs/privacy-plan.md` said the logging setup "loads candidate and contact
identity values from the database at startup and installs a redaction filter
for them". It did not. There was no `logging.Filter` anywhere in the tree, and
the sentence had been sitting in the privacy plan and in the compiled privacy
rule since spec 029. A public repository asserting a privacy control it never
built is worse than one that admits the gap, so the control is built here and
the sentence now names the test that proves it.

What this does and does not protect:

- Logs are never-in-git, so this is not what stops identity reaching the public
  repository; classification does that. This stops it reaching a log file that
  gets pasted into an issue, a terminal shared over a call, or a support
  bundle. That is a real path and it was undefended.
- Redaction is by literal value. A name spelled differently from the profile
  document, or a paraphrase, is not caught. This is a floor.
- The value set is refreshed from the one tracker write path (ADR-003) rather
  than read once at configure time. The API is long-lived, so a contact added
  after startup is the normal case, not the edge, and an earlier version left
  exactly that unredacted (review of PR #49). Refreshing at the write path
  rather than per log record keeps the database off the logging path, where a
  query that failed would log and recurse.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import cast

# A one-character value is not identifying and matches everywhere. Everything
# longer is redacted, but a short value only where it stands as its own word:
# that was the real reason for the old four-character floor, and a boundary
# match answers it without giving up the redaction (review of PR #49).
MIN_REDACTABLE_LENGTH = 2
WORD_BOUNDED_BELOW = 4

REDACTION = "[redacted]"

_IDENTITY_KEYS = ("name", "email", "phone", "linkedin")
_CONTACT_IDENTITY_COLUMNS = ("person_name", "person_email", "linkedin_url")


def _candidate_identity_values(conn: sqlite3.Connection) -> set[str]:
    import json

    row = conn.execute(
        "SELECT content FROM profile_documents WHERE kind = 'resume_data' AND format = 'json'"
    ).fetchone()
    if row is None:
        return set()
    try:
        parsed: object = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return set()
    if not isinstance(parsed, dict):
        return set()
    candidate = cast("dict[str, object]", parsed).get("candidate")
    if not isinstance(candidate, dict):
        return set()
    fields = cast("dict[str, object]", candidate)
    return {
        value.strip()
        for key in _IDENTITY_KEYS
        if isinstance(value := fields.get(key), str) and value.strip()
    }


def _contact_identity_values(conn: sqlite3.Connection) -> set[str]:
    columns = ", ".join(_CONTACT_IDENTITY_COLUMNS)
    values: set[str] = set()
    for row in conn.execute(f"SELECT {columns} FROM contacts"):
        for cell in cast("tuple[object, ...]", row):
            if isinstance(cell, str) and cell.strip():
                values.add(cell.strip())
    return values


def identity_values(conn: sqlite3.Connection) -> set[str]:
    """Every literal that names the candidate or a contact.

    Best effort by design: a database that predates these tables, or one that
    is mid-migration, must not stop the process from logging at all.
    """
    values: set[str] = set()
    for load in (_candidate_identity_values, _contact_identity_values):
        try:
            values |= load(conn)
        except sqlite3.Error:
            continue
    return {value for value in values if len(value) >= MIN_REDACTABLE_LENGTH}


class IdentityRedactionFilter(logging.Filter):
    """Replaces identity values in the rendered message.

    Attached to handlers rather than to the logger, because a logger's filters
    do not run on records that reach it by propagation, and nearly every record
    here propagates up from a module logger.

    The record is mutated rather than copied, so a second handler sees the
    redacted text too. That is intentional: the alternative redacts the file
    and leaves the terminal showing the value.
    """

    def __init__(self, values: set[str]) -> None:
        super().__init__()
        self._set_values(values)

    def _set_values(self, values: set[str]) -> None:
        # Longest first, so redacting an address does not leave the name behind
        # inside it, or vice versa.
        self._values = sorted(values, key=len, reverse=True)
        self._patterns = [
            (
                value,
                re.compile(rf"\b{re.escape(value)}\b") if len(value) < WORD_BOUNDED_BELOW else None,
            )
            for value in self._values
        ]

    def refresh(self, values: set[str]) -> None:
        """Replace the value set in place, so every handler already holding
        this filter starts redacting the new values immediately."""
        self._set_values(values)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._values:
            return True
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            return True
        redacted = message
        for value, bounded in self._patterns:
            if bounded is not None:
                redacted = bounded.sub(REDACTION, redacted)
            elif value in redacted:
                redacted = redacted.replace(value, REDACTION)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


# Every filter configure_logging installed, so a refresh reaches all of them.
# A list rather than a single slot because the CLI and the API each configure
# their own process, and tests configure repeatedly with force=True.
_INSTALLED: list[IdentityRedactionFilter] = []


def register(filter_: IdentityRedactionFilter) -> None:
    _INSTALLED.append(filter_)


def forget_all() -> None:
    """Drop the registry. Only `configure_logging(force=True)` and tests."""
    _INSTALLED.clear()


def refresh_installed(conn: sqlite3.Connection) -> None:
    """Re-read identity values and push them into every installed filter.

    Called from the tracker write path, so a contact is redactable from the
    moment it exists rather than from the next process start. Best effort for
    the same reason the initial load is: a refresh that fails must not break
    the write it was triggered by.
    """
    if not _INSTALLED:
        return
    try:
        values = identity_values(conn)
    except sqlite3.Error:
        return
    for installed in _INSTALLED:
        installed.refresh(values)
