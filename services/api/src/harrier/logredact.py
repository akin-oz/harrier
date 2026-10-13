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

`SecretScrubFilter`, at the end, is the other filter here: it keeps the store
password and other credential-shaped values out of the log stream (spec 133),
and a driver error's text, which quotes the row, out of every record (spec
135).
"""

from __future__ import annotations

import functools
import logging
import os
import re
import traceback
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import cast

from harrier.connection import (
    URL_VARIABLE,
    Connection,
    StoreDatabaseError,
    driver_error_text,
    is_driver_error,
    url_parts,
)

# A one-character value is not identifying and matches everywhere. Everything
# longer is redacted, but a short value only where it stands as its own word:
# that was the real reason for the old four-character floor, and a boundary
# match answers it without giving up the redaction (review of PR #49).
MIN_REDACTABLE_LENGTH = 2
WORD_BOUNDED_BELOW = 4

REDACTION = "[redacted]"

_IDENTITY_KEYS = ("name", "email", "phone", "linkedin")
_CONTACT_IDENTITY_COLUMNS = ("person_name", "person_email", "linkedin_url")


# The documents that carry the candidate's identity: the facts (spec 098), the
# single document they were split from while it is still stored, so no point
# in the split leaves a value unredacted, and the copy the split keeps, which
# can hold a value the facts have since replaced.
_IDENTITY_KINDS = ("resume_facts", "resume_data", "resume_data_presplit")


def _candidate_identity_values(conn: Connection) -> set[str]:
    import json

    placeholders = ", ".join("?" for _ in _IDENTITY_KINDS)
    rows = conn.execute(
        f"SELECT content FROM profile_documents WHERE kind IN ({placeholders}) AND format = 'json'",
        _IDENTITY_KINDS,
    ).fetchall()
    values: set[str] = set()
    for row in rows:
        try:
            parsed: object = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(parsed, dict):
            continue
        candidate = cast("dict[str, object]", parsed).get("candidate")
        if not isinstance(candidate, dict):
            continue
        fields = cast("dict[str, object]", candidate)
        values.update(
            value.strip()
            for key in _IDENTITY_KEYS
            if isinstance(value := fields.get(key), str) and value.strip()
        )
    return values


def _contact_identity_values(conn: Connection) -> set[str]:
    columns = ", ".join(_CONTACT_IDENTITY_COLUMNS)
    values: set[str] = set()
    for row in conn.execute(f"SELECT {columns} FROM contacts"):
        for cell in cast("tuple[object, ...]", row):
            if isinstance(cell, str) and cell.strip():
                values.add(cell.strip())
    return values


def identity_values(conn: Connection) -> set[str]:
    """Every literal that names the candidate or a contact.

    Best effort by design: a database that predates these tables, or one that
    is mid-migration, must not stop the process from logging at all.
    """
    values: set[str] = set()
    for load in (_candidate_identity_values, _contact_identity_values):
        try:
            values |= load(conn)
        except StoreDatabaseError:
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
        redacted = self._redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()

        # An exception's text is not in getMessage(), so logger.exception()
        # carried identity values straight past this filter (review of #50).
        #
        # Formatting exc_info here rather than leaving it to the formatter is
        # deliberate: logging.Formatter only derives exc_text when it is unset,
        # so filling it with a redacted string is what stops the raw traceback
        # being rendered downstream.
        if record.exc_info and not record.exc_text:
            record.exc_text = "".join(traceback.format_exception(*record.exc_info))
        if record.exc_text:
            record.exc_text = self._redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self._redact(record.stack_info)
        return True

    def _redact(self, text: str) -> str:
        for value, bounded in self._patterns:
            if bounded is not None:
                text = bounded.sub(REDACTION, text)
            elif value in text:
                text = text.replace(value, REDACTION)
        return text


# Every filter configure_logging installed, so a refresh reaches all of them.
# A list rather than a single slot because the CLI and the API each configure
# their own process, and tests configure repeatedly with force=True.
_INSTALLED: list[IdentityRedactionFilter] = []


def register(filter_: IdentityRedactionFilter) -> None:
    _INSTALLED.append(filter_)


def forget_all() -> None:
    """Drop the registry. Only `configure_logging(force=True)` and tests."""
    _INSTALLED.clear()


def refresh_installed(conn: Connection) -> None:
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
    except StoreDatabaseError:
        return
    for installed in _INSTALLED:
        installed.refresh(values)


# --- the store password and other secrets (spec 133) ---

SECRET_REDACTION = "REDACTED"


def _environment_url() -> str:
    """`HARRIER_DATABASE_URL` as `store_target` reads it: the process
    environment, then `.env` in the working directory."""
    if URL_VARIABLE in os.environ:
        return os.environ[URL_VARIABLE]
    try:
        cwd = os.getcwd()
    except OSError:
        return ""
    return _env_file_url(cwd)


@functools.lru_cache(maxsize=8)
def _env_file_url(cwd: str) -> str:
    # Read once per working directory, so a log record does not read a file.
    from harrier.envfile import read_env_file

    try:
        return read_env_file(Path(cwd) / ".env").get(URL_VARIABLE, "")
    except (OSError, ValueError):
        return ""


def _quoted_forms(form: str) -> tuple[str, ...]:
    """`form` as `repr` and `json.dumps` write it, without the quotes
    around it (spec 133, Amendment (2026-10-12): a password quoted by
    `repr` or `json.dumps`).

    `repr` escapes `'` only when the text it quotes holds both quote kinds,
    which the text around the password decides, so both readings. A suffix
    holding both forces the escaped one, and is cut off again.
    """
    import json

    escaped = repr(form + "'\"")[1:-4]
    return (
        escaped,
        escaped.replace("\\'", "'"),
        json.dumps(form)[1:-1],
        json.dumps(form, ensure_ascii=False)[1:-1],
    )


@functools.lru_cache(maxsize=8)
def _password_forms_of(url: str) -> tuple[str, ...]:
    if not url.strip():
        return ()
    try:
        forms = url_parts(url.strip()).passwords
    except Exception:
        # A URL `store_target` refuses: the process does not open it, and
        # nothing here may raise on the logging path.
        return ()
    return tuple(dict.fromkeys((*forms, *(q for form in forms for q in _quoted_forms(form)))))


def store_password_forms() -> tuple[str, ...]:
    """Every form of the configured store URL's password (spec 133, open
    decision 1): as written, percent-decoded, and as libpq reads it, each
    also as `repr` and `json.dumps` quote it.

    A driver can quote the password alone, outside any URL shape
    `scrub_secrets` knows, and the process knows its own password, so no
    shape needs guessing. Empty when the store is SQLite.
    """
    return _password_forms_of(_environment_url())


def _mask(text: str, forms: tuple[str, ...]) -> str:
    # The union of every occurrence of every form is replaced, so a form
    # overlapping another leaves no part of either (the rule
    # `harrier.pgstore._scrub` follows).
    spans: list[tuple[int, int]] = []
    for form in forms:
        start = text.find(form)
        while start >= 0:
            spans.append((start, start + len(form)))
            start = text.find(form, start + 1)
    if not spans:
        return text
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    kept: list[str] = []
    at = 0
    for start, end in merged:
        kept.append(text[at:start])
        kept.append(SECRET_REDACTION)
        at = end
    kept.append(text[at:])
    return "".join(kept)


def scrub_outbound(text: str) -> str:
    """Text as it may leave the process: every form of the configured store
    URL's password, then `scrub_secrets` (spec 133).

    The forms first, on the text as written. `scrub_secrets` replaces only
    the value after a `name=`, so run first on a password that holds such a
    shape (`...Passphrase=2026...`) it rewrote the password's tail, and the
    forms no longer matched what was left: the head leaked (privacy review
    of PR #296).
    """
    # Here, not at module import: `harrier.sources` reaches the tracker,
    # whose write path imports this module.
    from harrier.sources import scrub_secrets

    return scrub_secrets(_mask(text, store_password_forms()))


# --- a driver error's text (spec 135) ---


def _driver_error_shown(error: BaseException) -> str:
    """A driver error as a log argument prints it: the seam's text, or its
    class when the seam cannot render it."""
    return driver_error_text(error) or f"{type(error).__module__}.{type(error).__qualname__}"


def _withhold_driver_text(top: traceback.TracebackException, error: BaseException) -> None:
    """Replace the text of every driver error in a formatted chain.

    `TracebackException` builds its tree in step with the exception's:
    `__cause__`, `__context__` and a group's members, each built from the
    exception's own (`traceback.py`, `TracebackException.__init__`). So the
    two are walked together, and only a driver error's line changes: its
    type and frames stay, and so does every other exception's text.
    """
    pending: list[tuple[traceback.TracebackException, BaseException]] = [(top, error)]
    seen: set[int] = set()
    while pending:
        formatted, exception = pending.pop()
        if id(formatted) in seen:
            continue
        seen.add(id(formatted))
        if is_driver_error(exception):
            if not hasattr(formatted, "_str"):
                # A Python whose traceback keeps the text elsewhere: the
                # filter withholds the whole record rather than print it.
                raise RuntimeError("the exception text cannot be withheld")
            # Private to `traceback`, and what `format_exception_only`
            # prints after the type: set, not read, so no other text moves.
            setattr(formatted, "_str", driver_error_text(exception))  # noqa: B010
            formatted.__notes__ = None
        if formatted.__cause__ is not None and exception.__cause__ is not None:
            pending.append((formatted.__cause__, exception.__cause__))
        if formatted.__context__ is not None and exception.__context__ is not None:
            pending.append((formatted.__context__, exception.__context__))
        members = formatted.exceptions
        if members and isinstance(exception, BaseExceptionGroup):
            group = cast("BaseExceptionGroup[BaseException]", exception)
            pending.extend(zip(members, group.exceptions, strict=False))


def format_exception_withheld(
    exc_info: tuple[type[BaseException], BaseException, TracebackType | None]
    | tuple[None, None, None],
) -> str:
    """`logging.Formatter.formatException`, with every driver error in the
    chain printed as its type and `driver_error_text` (spec 135)."""
    error = exc_info[1]
    if error is None:
        return logging.Formatter().formatException(exc_info)
    formatted = traceback.TracebackException(type(error), error, error.__traceback__, compact=True)
    _withhold_driver_text(formatted, error)
    text = "".join(formatted.format())
    # As `formatException` does: no trailing newline.
    return text[:-1] if text.endswith("\n") else text


class SecretScrubFilter(logging.Filter):
    """Passes a record's message, exception text and stack text through
    `scrub_outbound` (spec 133), after withholding the text of every driver
    error in the record (spec 135).

    Separate from `IdentityRedactionFilter`, which does nothing without
    identity values and is not installed when they cannot be loaded: the two
    cases where a hosted store's password would pass unscrubbed. This one
    needs no database, so it is always installed, before the identity filter
    on the same handler.

    The record is mutated, as the identity filter mutates it, so every
    handler after the first sees the scrubbed text. The filter never raises:
    an exception from a filter reaches the code that logged.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            self._withhold_driver_arguments(record)
            self._scrub_message(record)
            # Formatted here, as `logging.Formatter.format` would, because
            # the formatter prints `exc_text` instead of formatting
            # `exc_info` again once it is set. Chained causes included, each
            # driver error among them without its text (spec 135).
            if record.exc_info and not record.exc_text:
                record.exc_text = format_exception_withheld(record.exc_info)
            if record.exc_text:
                record.exc_text = scrub_outbound(record.exc_text)
            if record.stack_info:
                record.stack_info = scrub_outbound(record.stack_info)
        except Exception:
            # Nothing could be checked: drop what could hold the secret
            # rather than let it through or break the caller.
            record.msg = "log record withheld: it could not be scrubbed"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True

    @staticmethod
    def _withhold_driver_arguments(record: logging.LogRecord) -> None:
        """A driver error logged as the message or as an argument renders
        as the seam's text, not as `str()`, which quotes the row (spec 135)."""
        if isinstance(record.msg, BaseException) and is_driver_error(record.msg):
            record.msg = _driver_error_shown(record.msg)
        args = record.args
        if isinstance(args, tuple) and any(is_driver_error(arg) for arg in args):
            record.args = tuple(
                _driver_error_shown(arg)
                if isinstance(arg, BaseException) and is_driver_error(arg)
                else arg
                for arg in args
            )
        elif isinstance(args, Mapping) and any(is_driver_error(arg) for arg in args.values()):
            record.args = {
                key: _driver_error_shown(arg)
                if isinstance(arg, BaseException) and is_driver_error(arg)
                else arg
                for key, arg in args.items()
            }

    @staticmethod
    def _scrub_message(record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            # A format string that does not match its arguments. Left as it
            # was, the handler's error report printed the raw arguments to
            # stderr (privacy review of PR #296). The format string is kept,
            # scrubbed, and the arguments are withheld, so the record renders.
            record.msg = (
                f"{scrub_outbound(str(record.msg))} "
                "(arguments withheld: they do not fit the format)"
            )
            record.args = ()
            record.__dict__.pop("color_message", None)
            return
        scrubbed = scrub_outbound(message)
        if scrubbed == message:
            return
        # The arguments one by one first, which keeps a formatter that reads
        # `record.args` itself working: uvicorn's access formatter unpacks
        # five of them, and its request line can carry a query secret.
        if isinstance(record.msg, str) and isinstance(record.args, tuple):
            args = tuple(
                scrub_outbound(arg) if isinstance(arg, str) else arg for arg in record.args
            )
            fmt = scrub_outbound(record.msg)
            try:
                rendered: str | None = fmt % args if args else fmt
            except (TypeError, ValueError):
                rendered = None
            if rendered is not None and scrub_outbound(rendered) == rendered:
                record.msg = fmt
                record.args = args
                return
        # A secret no argument holds alone, such as one in an exception
        # passed as an argument: the rendered message, and no argument left
        # to carry it.
        record.msg = scrubbed
        record.args = ()
        # uvicorn's coloured formatter renders this instead of `msg`.
        record.__dict__.pop("color_message", None)
