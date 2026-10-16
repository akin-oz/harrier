"""A log record prints the same whatever order its filters run in (spec 136).

Two filters change a record before a handler prints it: `SecretScrubFilter`
(specs 133, 135) and `IdentityRedactionFilter` (spec 045). Each rendered the
record itself, and the first to render it decided what the other and the
formatter saw. These tests build the handlers the way a caller could, with
either filter alone and with both in each order, and read what they print.

Every psycopg error is built in memory, so no Postgres server is needed. The
row value, the password and the identity value are synthetic and generated
per test.
"""

from __future__ import annotations

import io
import logging
import secrets
import uuid
from collections.abc import Callable, Generator
from typing import cast
from urllib.parse import quote

import psycopg
import pytest

from harrier.connection import StoreIntegrityError, store_target
from harrier.db import connect
from harrier.logredact import IdentityRedactionFilter, SecretScrubFilter
from harrier.logsetup import SECRET_SCRUB, configure_logging

URL_VARIABLE = "HARRIER_DATABASE_URL"

# The same for every handler compared, so a difference in the text is a
# difference in the filters and not in the clock.
FORMAT = "%(levelname)s %(name)s: %(message)s"
LOGGER_NAME = "harrier.tests.filter_order"

# A shape `scrub_secrets` reads as a parameter, so the idempotence test has
# one that is not the store password.
SECRET_PARAMETER = "token"


class Synthetic:
    """The values one test plants, none of them real."""

    def __init__(self) -> None:
        self.row = f"row-{uuid.uuid4().hex}"
        self.password = f"pw{secrets.token_hex(12)}"
        self.identity = f"Quillon Marrowby {uuid.uuid4().hex[:6]}"
        self.url = f"postgresql://harrier:{quote(self.password, safe='')}@db.example.test:5432/t"
        self.parameter = secrets.token_hex(10)

    def leaks(self, text: str) -> list[str]:
        return [
            value
            for value in (self.row, self.password, self.identity, self.parameter)
            if value in text
        ]


@pytest.fixture()
def synthetic(monkeypatch: pytest.MonkeyPatch) -> Synthetic:
    values = Synthetic()
    assert store_target({URL_VARIABLE: values.url}).is_postgres
    monkeypatch.setenv(URL_VARIABLE, values.url)
    return values


@pytest.fixture(autouse=True)
def _root_logging_restored(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:  # pyright: ignore[reportUnusedFunction]
    """`configure_logging(force=True)` replaces the root's handlers and sets
    the configured flag; both are put back, as the other logging tests do."""
    import harrier.logredact
    import harrier.logsetup

    root = logging.getLogger()
    before = list(root.handlers)
    monkeypatch.setattr(root, "handlers", list(before))
    monkeypatch.setattr(root, "level", root.level)
    monkeypatch.setattr(
        "harrier.logsetup._configured",
        harrier.logsetup._configured,  # pyright: ignore[reportPrivateUsage]
    )
    monkeypatch.setattr(
        "harrier.logredact._INSTALLED",
        list(harrier.logredact._INSTALLED),  # pyright: ignore[reportPrivateUsage]
    )
    yield
    for handler in root.handlers:
        if handler not in before:
            handler.close()


def a_driver_error(synthetic: Synthetic) -> psycopg.errors.CheckViolation:
    """A psycopg error whose text quotes a row and the store password, as the
    server's `DETAIL` line can."""
    return psycopg.errors.CheckViolation(
        'new row for relation "jobs" violates check constraint "x"\n'
        f"DETAIL:  Failing row contains ({synthetic.row}, {synthetic.password})."
    )


def logged_as_an_exception(synthetic: Synthetic) -> Callable[[logging.Logger], None]:
    """`logger.exception` for a store error chained to the driver's, under an
    error whose own text holds the password and the identity value, with a
    message that holds the identity value."""

    def log(logger: logging.Logger) -> None:
        try:
            try:
                try:
                    raise a_driver_error(synthetic)
                except psycopg.Error as driver:
                    raise StoreIntegrityError("check", "refused") from driver
            except StoreIntegrityError as store:
                raise RuntimeError(
                    f"{synthetic.identity} could not be saved to {synthetic.url}"
                ) from store
        except RuntimeError:
            logger.exception("saving %s failed", synthetic.identity)

    return log


def logged_as_an_argument(synthetic: Synthetic) -> Callable[[logging.Logger], None]:
    """A psycopg error passed as an argument to a message that holds the
    identity value."""

    def log(logger: logging.Logger) -> None:
        logger.error("saving %s failed: %s", synthetic.identity, a_driver_error(synthetic))

    return log


def printed_through(
    handler: logging.StreamHandler[io.StringIO], log: Callable[[logging.Logger], None]
) -> str:
    """What `handler` prints for one call of `log`: the handler is attached to
    a logger of its own, with its stream and formatter fixed."""
    buffer = io.StringIO()
    handler.setStream(buffer)
    handler.setFormatter(logging.Formatter(FORMAT))
    logger = logging.getLogger(LOGGER_NAME)
    previous = (logger.handlers[:], logger.propagate, logger.level)
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    try:
        log(logger)
    finally:
        logger.handlers[:], logger.propagate = previous[0], previous[1]
        logger.setLevel(previous[2])
    return buffer.getvalue()


def a_handler_with(*filters: logging.Filter) -> logging.StreamHandler[io.StringIO]:
    handler = logging.StreamHandler(io.StringIO())
    for each in filters:
        handler.addFilter(each)
    return handler


def the_identity_filter(synthetic: Synthetic) -> IdentityRedactionFilter:
    return IdentityRedactionFilter({synthetic.identity})


def test_the_identity_filter_alone_withholds_what_the_scrub_withholds(
    synthetic: Synthetic,
) -> None:
    written = printed_through(
        a_handler_with(the_identity_filter(synthetic)), logged_as_an_exception(synthetic)
    )

    assert synthetic.leaks(written) == []
    # The records still say what failed: the chain is printed.
    assert "saving [redacted] failed" in written
    assert "psycopg.errors.CheckViolation" in written
    assert "harrier.connection.StoreIntegrityError" in written


def test_the_identity_filter_alone_withholds_a_driver_argument(synthetic: Synthetic) -> None:
    written = printed_through(
        a_handler_with(the_identity_filter(synthetic)), logged_as_an_argument(synthetic)
    )

    assert synthetic.leaks(written) == []
    assert "saving [redacted] failed: " in written


def test_filter_order_does_not_change_what_a_record_prints(synthetic: Synthetic) -> None:
    conn = connect()
    try:
        with conn.transaction():
            conn.execute("INSERT INTO contacts (person_name) VALUES (?)", (synthetic.identity,))
    finally:
        conn.close()
    configure_logging(force=True)
    installed = next(
        cast("logging.StreamHandler[io.StringIO]", handler)
        for handler in logging.getLogger().handlers
        if isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
    )
    assert SECRET_SCRUB in installed.filters
    assert any(isinstance(each, IdentityRedactionFilter) for each in installed.filters)

    for name, log in (
        ("an exception", logged_as_an_exception(synthetic)),
        ("an argument", logged_as_an_argument(synthetic)),
    ):
        scrub_first = printed_through(
            a_handler_with(SecretScrubFilter(), the_identity_filter(synthetic)), log
        )
        identity_first = printed_through(
            a_handler_with(the_identity_filter(synthetic), SecretScrubFilter()), log
        )
        configured = printed_through(installed, log)

        assert synthetic.leaks(configured) == [], name
        assert scrub_first == identity_first, name
        assert scrub_first == configured, name


def test_the_secret_scrub_is_idempotent(synthetic: Synthetic) -> None:
    def record() -> logging.LogRecord:
        try:
            try:
                raise a_driver_error(synthetic)
            except psycopg.Error as driver:
                raise StoreIntegrityError("check", "refused") from driver
        except StoreIntegrityError:
            import sys

            made = logging.LogRecord(
                LOGGER_NAME,
                logging.ERROR,
                __file__,
                1,
                "connect %s with %s=%s failed: %s: %s",
                (
                    synthetic.url,
                    SECRET_PARAMETER,
                    synthetic.parameter,
                    a_driver_error(synthetic),
                    # Not a string and not the driver's: no argument is
                    # scrubbed alone, so the scrub rewrites the whole message.
                    RuntimeError(f"refused {synthetic.url}"),
                ),
                sys.exc_info(),
            )
        return made

    formatter = logging.Formatter(FORMAT)
    once = record()
    SecretScrubFilter().filter(once)
    twice = record()
    SecretScrubFilter().filter(twice)
    SecretScrubFilter().filter(twice)

    printed_once = formatter.format(once)
    assert synthetic.leaks(printed_once) == []
    assert "REDACTED" in printed_once
    assert formatter.format(twice) == printed_once
