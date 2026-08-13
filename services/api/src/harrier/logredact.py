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
- The values are read once, at configure time. A contact added later in the
  same process is not redacted until the next start. Reading per record would
  put a database query on every log call, which is the wrong trade for a
  process that logs on failure paths.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import cast

# Below this length a "value" is not identifying and redacting it would shred
# unrelated lines: a one-letter name, an empty column, a bare initial.
MIN_REDACTABLE_LENGTH = 4

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
        # Longest first, so redacting an address does not leave the name behind
        # inside it, or vice versa.
        self._values = sorted(values, key=len, reverse=True)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._values:
            return True
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            return True
        redacted = message
        for value in self._values:
            if value in redacted:
                redacted = redacted.replace(value, REDACTION)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True
