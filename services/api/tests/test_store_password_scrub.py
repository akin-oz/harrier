"""The store password stays out of logs and HTTP errors (spec 133).

Each test drives a sink the way the process does: a record through the
handlers `configure_logging` and `create_app` filter, an exception through a
real uvicorn server, a refusal through the app's own exception handlers. None
calls the filter or the scrub directly, so each fails when the installation
it proves is removed, not only when the scrub itself breaks.

The password is made at run time and never written in this file. A check
looks for every 8-character window of both forms of it, so a scrub that left
part of it behind fails as surely as one that left all of it.

Pyright strict cannot resolve starlette's TestClient response types, so the
three unknown-type rules are off for this file only, as in test_api_jobs.py.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import io
import json
import logging
import secrets
import string
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import quote

import pytest
import uvicorn
import uvicorn.config
import uvicorn.logging
from conftest import auth
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from harrier.connection import StoreDatabaseError, store_target
from harrier.db import DatabaseOwnedByContainer, connect, default_db_path
from harrier.logredact import IdentityRedactionFilter
from harrier.logsetup import UVICORN_LOGGERS, configure_logging, log_path
from harrier_api.app import create_app
from harrier_api.runs import RunConflictError

URL_VARIABLE = "HARRIER_DATABASE_URL"


def _synthetic(alphabet: str, length: int) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _windows(secret: str) -> set[str]:
    return {secret[i : i + 8] for i in range(len(secret) - 7)}


@dataclass(frozen=True)
class StorePassword:
    """A password made at run time, and the URL that carries it."""

    decoded: str
    encoded: str
    url: str
    # The password alone as the driver's message quotes it; empty for
    # plain double quotes.
    quoted: str = ""

    def leaks(self, text: str) -> list[str]:
        """Every 8-character window of either form found in `text`."""
        return sorted(
            window
            for form in (self.decoded, self.encoded)
            for window in _windows(form)
            if window in text
        )


@pytest.fixture(autouse=True)
def _root_logging_restored(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:  # pyright: ignore[reportUnusedFunction]
    """`configure_logging(force=True)` binds the root's stderr handler to
    this test's captured stream, which is closed after it. Left in place, a
    later test that logs gets a logging error in its own stderr. The root's
    handlers, level and the configured flag are put back."""
    import harrier.logredact
    import harrier.logsetup

    root = logging.getLogger()
    before = list(root.handlers)
    monkeypatch.setattr(root, "handlers", list(before))
    monkeypatch.setattr(root, "level", root.level)
    configured = harrier.logsetup._configured  # pyright: ignore[reportPrivateUsage]
    installed = harrier.logredact._INSTALLED  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr("harrier.logsetup._configured", configured)
    monkeypatch.setattr("harrier.logredact._INSTALLED", list(installed))
    yield
    for handler in root.handlers:
        if handler not in before:
            handler.close()


@pytest.fixture()
def store_password(monkeypatch: pytest.MonkeyPatch) -> StorePassword:
    return _made_password("%'\" @:/")


def _made_password(specials: str) -> StorePassword:
    # Plain runs around characters a URL cannot carry raw, so the URL holds
    # it percent-encoded and a driver can quote either form. The middle
    # holds the shapes `scrub_secrets` reads as a parameter (`pass=`,
    # `token=`, `secret=`), so a scrub that reads them before masking the
    # password splits it and leaves its head (privacy review of PR #296).
    plain = string.ascii_letters + string.digits
    decoded = (
        f"{_synthetic(plain, 16)}pass={_synthetic(plain, 8)}&token={_synthetic(plain, 8)}"
        f"?secret={_synthetic(plain, 8)}{specials}{_synthetic(plain, 16)}"
    )
    encoded = quote(decoded, safe="")
    url = f"postgresql://harrier:{encoded}@db.example.test:5432/tracker?sslmode=require"
    # A URL the process would open, not one only this test can build.
    assert store_target({URL_VARIABLE: url}).is_postgres
    return StorePassword(decoded=decoded, encoded=encoded, url=url)


def _point_at_postgres(monkeypatch: pytest.MonkeyPatch, password: StorePassword) -> None:
    # Set after logging is configured and the app is built, both of which
    # run on the local store today (spec 103): the scrub reads the URL when
    # it runs, which is what lets it follow the store spec 134 opens.
    monkeypatch.setenv(URL_VARIABLE, password.url)


class OperationalError(Exception):
    """A stand-in for the driver's error: its text quotes the URL, and the
    password alone, as a driver message can."""


def _driver_error(password: StorePassword) -> OperationalError:
    # The decoded form alone, and the form as written alone, as libpq quotes
    # a URL it refuses still percent-encoded (`UrlParts`), outside the URL
    # shape `scrub_secrets` knows.
    alone = password.quoted or f'"{password.decoded}"'
    return OperationalError(
        f'connection to "{password.url}" failed: password {alone} was refused'
        f" (as written: {password.encoded})"
    )


def _store_error(password: StorePassword) -> StoreDatabaseError:
    """The seam's error as domain code sees it: its own message quotes
    nothing, and the driver's error is chained as its cause (spec 112)."""
    try:
        try:
            raise _driver_error(password)
        except OperationalError as cause:
            raise StoreDatabaseError("the store refused the connection") from cause
    except StoreDatabaseError as error:
        return error


# --- uvicorn, as its default configuration builds it ---


@pytest.fixture()
def uvicorn_streams(monkeypatch: pytest.MonkeyPatch) -> tuple[io.StringIO, io.StringIO]:
    """uvicorn's two handlers, shaped as `uvicorn/config.py` (LOGGING_CONFIG)
    builds them, writing to buffers. Built by hand rather than with
    `dictConfig`, which closes every handler in the process. Restored after
    the test by monkeypatch."""
    # The loggers uvicorn gives handlers, read from uvicorn itself, so an
    # upgrade that moved one fails here rather than in production.
    with_handlers = tuple(
        name
        for name, config in uvicorn.config.LOGGING_CONFIG["loggers"].items()
        if config.get("handlers")
    )
    assert with_handlers == UVICORN_LOGGERS
    errors, access = io.StringIO(), io.StringIO()
    default = logging.StreamHandler(errors)
    default.setFormatter(
        uvicorn.logging.DefaultFormatter("%(levelprefix)s %(message)s", use_colors=False)
    )
    access_handler = logging.StreamHandler(access)
    access_handler.setFormatter(
        uvicorn.logging.AccessFormatter(
            '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
            use_colors=False,
        )
    )
    for name, handler in (("uvicorn", default), ("uvicorn.access", access_handler)):
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "handlers", [handler])
        monkeypatch.setattr(logger, "propagate", False)
        monkeypatch.setattr(logger, "level", logging.INFO)
    monkeypatch.setattr(logging.getLogger("uvicorn.error"), "level", logging.INFO)
    return errors, access


@contextmanager
def _serving(app: FastAPI) -> Generator[str]:
    """The app behind a real uvicorn server on the h11 protocol, the one
    whose "Exception in ASGI application" record spec 133 names. No log
    configuration of its own: the handlers above are uvicorn's."""
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, http="h11", lifespan="off", log_config=None
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(15)
        assert not thread.is_alive(), "uvicorn did not stop"


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8")


# --- one case per sink ---


def _through_a_root_handler(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    password: StorePassword,
    streams: tuple[io.StringIO, io.StringIO],
) -> str:
    """The stderr handler and the log file, with the chained driver error,
    the URL as an argument and the driver's error as an argument."""
    configure_logging(force=True)
    _point_at_postgres(monkeypatch, password)
    logger = logging.getLogger("harrier.tests.store_password")
    error = _store_error(password)
    try:
        raise error
    except StoreDatabaseError:
        logger.exception("reading the tracker failed")
    logger.error("opening %s", password.url)
    logger.error("the driver said: %s", error.__cause__)
    # Stack text, as a record built elsewhere can carry it (a queue or
    # socket handler's record): the live stack never holds a value.
    logger.handle(
        logger.makeRecord(
            logger.name,
            logging.ERROR,
            __file__,
            0,
            "a record with stack text",
            (),
            None,
            sinfo=f"Stack (most recent call last):\n  connect({password.url!r})",
        )
    )
    for handler in logging.getLogger().handlers:
        handler.flush()
    stderr = capsys.readouterr().err
    logged = log_path().read_text(encoding="utf-8")
    for written in (stderr, logged):
        assert "OperationalError" in written, "the chained driver error was not logged"
        # The user and the host too, in a log record (spec 145).
        assert "opening postgresql://REDACTED:REDACTED@REDACTED:5432/tracker" in written
        assert "the driver said: connection to" in written
        assert "connect('postgresql://REDACTED:REDACTED@REDACTED:5432/tracker" in written
        # Nowhere: the message, the chained exception's text and the stack
        # text (spec 145; local review of PR #360).
        assert "db.example.test" not in written
    return stderr + logged


def _through_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    password: StorePassword,
    streams: tuple[io.StringIO, io.StringIO],
) -> str:
    """An unhandled exception, logged by uvicorn on its own handler."""
    app = create_app()

    def unhandled() -> None:
        raise RuntimeError("the request failed") from _store_error(password)

    app.add_api_route("/spec-133/unhandled", unhandled)
    _point_at_postgres(monkeypatch, password)
    with _serving(app) as base:
        status, body = _get(f"{base}/spec-133/unhandled")
    assert (status, body) == (500, "Internal Server Error")
    errors = streams[0].getvalue()
    assert "Exception in ASGI application" in errors
    assert "OperationalError" in errors, "the chained driver error was not logged"
    return body + errors


def _through_an_http_exception(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    password: StorePassword,
    streams: tuple[io.StringIO, io.StringIO],
) -> str:
    """`HTTPException(detail=str(error))`, the shape of every raise site."""
    app = create_app()

    def refused() -> None:
        error = _store_error(password)
        raise HTTPException(status_code=409, detail=str(error.__cause__)) from error

    app.add_api_route("/spec-133/refused", refused)
    _point_at_postgres(monkeypatch, password)
    response = TestClient(app).get("/spec-133/refused")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail.startswith('connection to "postgresql://harrier:REDACTED@db.example.test')
    # The detail as decoded, since JSON escapes the password's quote.
    return response.json()["detail"] + response.text


class _ConflictQuotingTheStore(RunConflictError):
    def __init__(self, text: str) -> None:  # pyright: ignore[reportMissingSuperCall]
        Exception.__init__(self, text)


def _through_run_conflict(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    password: StorePassword,
    streams: tuple[io.StringIO, io.StringIO],
) -> str:
    """`create_app`'s `run_conflict` handler, which builds its own body."""
    app = create_app()

    def conflicting() -> None:
        raise _ConflictQuotingTheStore(str(_driver_error(password)))

    app.add_api_route("/spec-133/conflict", conflicting)
    _point_at_postgres(monkeypatch, password)
    response = TestClient(app).get("/spec-133/conflict")
    assert response.status_code == 409
    assert response.json()["detail"].startswith('connection to "postgresql://harrier:REDACTED@')
    # The detail as decoded, since JSON escapes the password's quote.
    return response.json()["detail"] + response.text


Sink = Callable[
    [
        pytest.MonkeyPatch,
        pytest.CaptureFixture[str],
        StorePassword,
        tuple[io.StringIO, io.StringIO],
    ],
    str,
]

SINKS: dict[str, Sink] = {
    "root handler": _through_a_root_handler,
    "uvicorn.error": _through_uvicorn,
    "HTTPException detail": _through_an_http_exception,
    "run_conflict": _through_run_conflict,
}


@pytest.mark.parametrize("sink", list(SINKS))
def test_no_log_or_http_error_carries_the_store_password(
    sink: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    store_password: StorePassword,
    uvicorn_streams: tuple[io.StringIO, io.StringIO],
) -> None:
    """With the password in HARRIER_DATABASE_URL, no 8-character window of
    it, written or decoded, leaves the process through the sink."""
    out = SINKS[sink](monkeypatch, capsys, store_password, uvicorn_streams)

    assert "REDACTED" in out
    assert store_password.leaks(out) == []


# A message that quotes the password alone with `!r` or as JSON escapes
# some of its characters, so the text it writes is none of the forms the
# URL lists (spec 133, Amendment (2026-10-12): a password quoted by `repr`
# or `json.dumps`).
QUOTINGS: dict[str, Callable[[str], str]] = {
    "repr": repr,
    "repr in a dict": lambda password: repr({"until": password}),
    "json.dumps": json.dumps,
    # As Starlette writes a JSON body.
    "json.dumps, non-ASCII as is": lambda password: json.dumps(
        {"until": password}, ensure_ascii=False
    ),
}

# The fixture's password holds both quote kinds, so `repr` writes its `'`
# as `\\'`. This one holds no `"`, so `repr` leaves its `'` as it is and
# escapes the backslash and the control character, and only the default
# `json.dumps` escapes the non-ASCII character.
ONE_QUOTE = "%' \\\x01\u00e9@:/"


@pytest.mark.parametrize("specials", [None, ONE_QUOTE], ids=["both quotes", "one quote"])
@pytest.mark.parametrize("quoting", list(QUOTINGS))
@pytest.mark.parametrize("sink", list(SINKS))
def test_a_password_quoted_by_repr_or_json_is_masked_at_every_sink(
    sink: str,
    quoting: str,
    specials: str | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    store_password: StorePassword,
    uvicorn_streams: tuple[io.StringIO, io.StringIO],
) -> None:
    """No 8-character window of the password leaves the process through
    the sink, however the message quoted it."""
    made = store_password if specials is None else _made_password(specials)
    quoted = QUOTINGS[quoting](made.decoded)
    assert made.decoded not in quoted, "the quoting escaped nothing"
    password = replace(made, quoted=quoted)

    out = SINKS[sink](monkeypatch, capsys, password, uvicorn_streams)

    assert "REDACTED" in out
    assert password.leaks(out) == []


# `ascii()`, `!a` and `%a` write the password as `repr` does, except that
# each non-ASCII character becomes an escape of its width, so the text is
# none of the `repr` or JSON forms (spec 133, Amendment (2026-10-15): a
# password quoted by `ascii()`).
ASCII_QUOTINGS: dict[str, Callable[[str], str]] = {
    "ascii()": ascii,
    # The `%a` format itself is the case, so not a format specifier.
    "%a": lambda password: "%a" % (password,),  # noqa: UP031
    "!a in a dict": lambda password: f"{ {'until': password}!a}",
}

# One non-ASCII character of each escape width: `\xHH`, `\uHHHH` and
# `\UHHHHHHHH`. Without `"`, `ascii()` leaves the `'` as it is; with it,
# `ascii()` writes `\'`.
EVERY_WIDTH = "%' \\\u00e9\u20ac\U0001f600@:/"
EVERY_WIDTH_BOTH_QUOTES = EVERY_WIDTH + '"'


@pytest.mark.parametrize(
    "specials", [EVERY_WIDTH_BOTH_QUOTES, EVERY_WIDTH], ids=["both quotes", "one quote"]
)
@pytest.mark.parametrize("quoting", list(ASCII_QUOTINGS))
@pytest.mark.parametrize("sink", list(SINKS))
def test_a_password_quoted_by_ascii_is_masked_at_every_sink(
    sink: str,
    quoting: str,
    specials: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    uvicorn_streams: tuple[io.StringIO, io.StringIO],
) -> None:
    """No 8-character window of the password leaves the process through
    the sink when the message quoted it with `ascii()`."""
    made = _made_password(specials)
    quoted = ASCII_QUOTINGS[quoting](made.decoded)
    for escape in ("\\xe9", "\\u20ac", "\\U0001f600"):
        assert escape in quoted, f"the quoting did not write {escape}"
    password = replace(made, quoted=quoted)

    out = SINKS[sink](monkeypatch, capsys, password, uvicorn_streams)

    assert "REDACTED" in out
    assert password.leaks(out) == []


# --- the filter needs no identity values ---


def _no_identity_values(monkeypatch: pytest.MonkeyPatch) -> str | None:
    # A fresh store: the identity filter is installed and holds nothing, so
    # the warning that it is unavailable must not appear.
    return None


def _an_unreadable_database(monkeypatch: pytest.MonkeyPatch) -> str:
    path = default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"this is not a SQLite database, not even its header" * 100)
    return "identity redaction is unavailable"


def _ownership_refused(monkeypatch: pytest.MonkeyPatch) -> str:
    def refused() -> set[str] | None:
        raise DatabaseOwnedByContainer("the harrier container owns the database")

    monkeypatch.setattr("harrier.logsetup._load_identity_values", refused)
    return "logging to stderr only, without identity redaction"


@pytest.mark.parametrize(
    "arrange",
    [_no_identity_values, _an_unreadable_database, _ownership_refused],
    ids=["no identity values", "database unreadable", "ownership refused"],
)
def test_the_scrub_filter_needs_no_identity_values(
    arrange: Callable[[pytest.MonkeyPatch], str | None],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    store_password: StorePassword,
) -> None:
    """The identity filter does nothing without values and is not installed
    when they cannot be loaded. The scrub is installed on every path."""
    warning = arrange(monkeypatch)
    if warning is None:
        configure_logging(force=True)
        identity = [
            found
            for handler in logging.getLogger().handlers
            for found in handler.filters
            if isinstance(found, IdentityRedactionFilter)
        ]
        assert identity, "the identity filter was not installed"
        assert "identity redaction is unavailable" not in capsys.readouterr().err
    configure_logging(force=True)
    _point_at_postgres(monkeypatch, store_password)

    try:
        raise RuntimeError("the request failed") from _store_error(store_password)
    except RuntimeError:
        logging.getLogger("harrier.tests.store_password").exception(
            "opening %s", store_password.url
        )

    written = capsys.readouterr().err
    if warning is not None:
        assert warning in written
    assert "OperationalError" in written
    assert store_password.leaks(written) == []


def test_the_scrub_runs_before_the_identity_filter(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    store_password: StorePassword,
) -> None:
    """A contact value that is part of the password: redacted first, it
    splits the password, and the parts on either side no longer match a
    form. The scrub sees the record first (Behavior)."""
    conn = connect()
    with conn.transaction():
        conn.execute(
            "INSERT INTO contacts (person_name) VALUES (?)", (store_password.decoded[4:14],)
        )
    conn.close()
    configure_logging(force=True)
    _point_at_postgres(monkeypatch, store_password)

    logging.getLogger("harrier.tests.store_password").error(
        "the driver said: %s", _driver_error(store_password)
    )

    written = capsys.readouterr().err
    assert "the driver said: connection to" in written
    assert store_password.leaks(written) == []


def test_every_root_handler_scrubs_a_record_on_its_own(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    store_password: StorePassword,
) -> None:
    """A record that reaches one handler alone is scrubbed there. Through
    the logger, the first handler's filter scrubs the record for the rest,
    which would hide a handler installed without the filter."""
    configure_logging(force=True)
    _point_at_postgres(monkeypatch, store_password)
    logger = logging.getLogger("harrier.tests.store_password")
    handlers = logging.getLogger().handlers
    assert len(handlers) == 2, "expected the stderr and the log file handlers"

    for handler in handlers:
        record = logger.makeRecord(
            logger.name, logging.ERROR, __file__, 0, "opening %s", (store_password.url,), None
        )
        handler.handle(record)
        handler.flush()

    stderr = capsys.readouterr().err
    logged = log_path().read_text(encoding="utf-8")
    for written in (stderr, logged):
        # The user and the host too, in a log record (spec 145).
        assert "opening postgresql://REDACTED:REDACTED@REDACTED:5432/tracker" in written
        assert "db.example.test" not in written
        assert store_password.leaks(written) == []


@pytest.mark.dotenv_fallback
def test_a_password_set_only_in_dotenv_is_scrubbed_as_well(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    store_password: StorePassword,
) -> None:
    """`store_target` reads `.env` when the variable is not exported, so the
    scrub reads the password where the store is chosen from."""
    configure_logging(force=True)
    # Into tmp_path before the variable goes, so no other `.env` is read.
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"{URL_VARIABLE}={store_password.url}\n", encoding="utf-8")
    monkeypatch.delenv(URL_VARIABLE)

    logging.getLogger("harrier.tests.store_password").error(
        "the driver said: %s", _driver_error(store_password)
    )

    written = capsys.readouterr().err
    assert "the driver said: connection to" in written
    assert store_password.leaks(written) == []


# --- what is not scrubbed stays as it was ---


def _routes(app: FastAPI) -> FastAPI:
    def plain() -> None:
        raise HTTPException(
            status_code=409,
            detail="the tracker refused the change: postgresql://harrier@db.example.test/tracker",
            headers={"X-Refusal": "kept"},
        )

    def structured() -> None:
        raise HTTPException(status_code=400, detail={"field": "kind", "problems": ["unknown"]})

    def bodiless() -> None:
        raise HTTPException(status_code=304, headers={"ETag": '"spec-133"'})

    def counted(count: int) -> dict[str, int]:
        return {"count": count}

    app.add_api_route("/spec-133/plain", plain)
    app.add_api_route("/spec-133/structured", structured)
    app.add_api_route("/spec-133/bodiless", bodiless)
    app.add_api_route("/spec-133/counted", counted)
    return app


@pytest.mark.parametrize(
    "path",
    [
        "/spec-133/plain",
        "/spec-133/structured",
        "/spec-133/bodiless",
        "/spec-133/counted?count=many",
        "/spec-133/no-such-route",
    ],
    ids=["string detail", "non-string detail", "no body", "422", "404"],
)
def test_a_detail_with_nothing_to_scrub_is_unchanged(
    path: str, monkeypatch: pytest.MonkeyPatch, store_password: StorePassword
) -> None:
    """Answered byte for byte as FastAPI answers it with no handler of
    harrier's, status and headers included, with a store password set."""
    scrubbing = _routes(create_app())
    before = _routes(FastAPI())
    _point_at_postgres(monkeypatch, store_password)

    ours = TestClient(scrubbing).get(path)
    theirs = TestClient(before).get(path)

    assert ours.status_code == theirs.status_code
    assert ours.content == theirs.content
    assert sorted(ours.headers.items()) == sorted(theirs.headers.items())


# --- what uvicorn's formatters need from a record ---


def test_an_access_line_with_a_secret_in_its_query_keeps_its_shape(
    monkeypatch: pytest.MonkeyPatch,
    store_password: StorePassword,
    uvicorn_streams: tuple[io.StringIO, io.StringIO],
) -> None:
    """uvicorn's access formatter unpacks the record's five arguments, so a
    request line scrubbed into the message with no arguments left would be
    lost as a logging error. The arguments are scrubbed one by one."""
    app = create_app()

    def ping() -> dict[str, bool]:
        return {"ok": True}

    app.add_api_route("/spec-133/ping", ping)
    _point_at_postgres(monkeypatch, store_password)
    with _serving(app) as base:
        status, _ = _get(f"{base}/spec-133/ping?password={store_password.encoded}&sslmode=require")
    assert status == 200

    access = uvicorn_streams[1].getvalue()
    assert '"GET /spec-133/ping?password=REDACTED&sslmode=require HTTP/1.1" 200' in access
    assert store_password.leaks(access) == []


def test_a_coloured_record_scrubbed_whole_is_still_rendered(
    monkeypatch: pytest.MonkeyPatch,
    store_password: StorePassword,
    uvicorn_streams: tuple[io.StringIO, io.StringIO],
) -> None:
    """uvicorn's coloured formatter renders `color_message` with the
    record's arguments. A record scrubbed whole has none left, so the
    coloured format is dropped and the scrubbed message is rendered."""
    handler = logging.getLogger("uvicorn").handlers[0]
    handler.setFormatter(
        uvicorn.logging.DefaultFormatter("%(levelprefix)s %(message)s", use_colors=True)
    )
    create_app()
    _point_at_postgres(monkeypatch, store_password)

    logging.getLogger("uvicorn.error").error(
        "the driver said: %s",
        _driver_error(store_password),
        extra={"color_message": "the driver said: \x1b[1m%s\x1b[0m"},
    )

    written = uvicorn_streams[0].getvalue()
    assert "the driver said: connection to" in written
    assert "%s" not in written
    assert store_password.leaks(written) == []


def test_a_record_that_cannot_be_scrubbed_is_withheld(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    store_password: StorePassword,
) -> None:
    """A filter that raises reaches the code that logged. One that cannot
    scrub withholds the record's text instead, and the caller carries on."""
    configure_logging(force=True)
    _point_at_postgres(monkeypatch, store_password)

    def broken(_text: str) -> str:
        raise RuntimeError("the scrub failed")

    # What the filter calls, which masks the store host too (spec 145).
    monkeypatch.setattr("harrier.logredact.scrub_logged", broken)
    logger = logging.getLogger("harrier.tests.store_password")
    logger.error("opening %s", store_password.url)
    # A record with the chained driver error, and one with stack text: what
    # is withheld includes both.
    try:
        raise RuntimeError("the request failed") from _store_error(store_password)
    except RuntimeError:
        logger.exception("reading the tracker failed")
    logger.handle(
        logger.makeRecord(
            logger.name,
            logging.ERROR,
            __file__,
            0,
            "a record with stack text",
            (),
            None,
            sinfo=f"Stack (most recent call last):\n  connect({store_password.url!r})",
        )
    )

    written = capsys.readouterr().err
    assert written.count("log record withheld: it could not be scrubbed") == 3
    assert "OperationalError" not in written
    assert "Stack (most recent call last)" not in written
    assert store_password.leaks(written) == []


def test_a_log_call_whose_arguments_do_not_fit_leaks_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    store_password: StorePassword,
) -> None:
    """A format string that does not fit its arguments fails to render, and
    logging's error report printed the raw arguments to stderr (privacy
    review of PR #296). The arguments are withheld and the record renders."""
    configure_logging(force=True)
    _point_at_postgres(monkeypatch, store_password)

    logging.getLogger("harrier.tests.store_password").error("opening %s on %s", store_password.url)

    written = capsys.readouterr().err
    assert "--- Logging error ---" not in written
    assert "opening %s on %s (arguments withheld: they do not fit the format)" in written
    assert store_password.leaks(written) == []


def test_a_command_that_skips_logging_setup_still_scrubs_its_warnings(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    store_password: StorePassword,
) -> None:
    """`harrier store` and `harrier doctor` never call `configure_logging`,
    so a warning, the driver's own included, goes to logging's last-resort
    handler. The CLI scrubs that handler before any command runs."""
    from harrier_cli.main import main

    last_resort = logging.lastResort
    assert last_resort is not None
    monkeypatch.setattr(last_resort, "filters", [])
    # A process in which nothing configured the root, as `harrier store` is.
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    with pytest.raises(SystemExit):
        main(["store", "--help"])
    capsys.readouterr()
    _point_at_postgres(monkeypatch, store_password)

    # The shape psycopg logs when a rollback fails (psycopg/connection.py).
    logging.getLogger("psycopg").warning(
        "error ignored in rollback on %s: %s",
        "<psycopg.Connection>",
        _driver_error(store_password),
    )

    written = capsys.readouterr().err
    assert "error ignored in rollback" in written
    assert store_password.leaks(written) == []


def test_a_run_event_has_the_store_password_masked(
    monkeypatch: pytest.MonkeyPatch, store_password: StorePassword
) -> None:
    """A run's output reaches the event stream through the run manager's
    scrub, which masks the password a run prints outside a URL too."""
    import asyncio

    from harrier_api.runs import Run, RunManager

    manager = RunManager()
    run = Run(id="r1", kind="demo", command=["true"])
    _point_at_postgres(monkeypatch, store_password)

    async def append() -> None:
        await manager._append(  # pyright: ignore[reportPrivateUsage]
            run, "log_line", {"line": str(_driver_error(store_password))}
        )

    asyncio.run(append())

    stored = str(run.events[-1].data["line"])
    assert stored.startswith('connection to "postgresql://harrier:REDACTED@db.example.test')
    assert store_password.leaks(stored) == []


# --- an error field in a 200 body -------------------------------------------


def _a_stored_hold_quoting(root: Path, value: str) -> str:
    """A stored row written around `set_config`, as a restored backup or a
    hand-edited database writes one, with an unknown key holding `value`.
    `ConfigError` quotes the entry with `!r`."""
    conn = connect()
    try:
        with conn.transaction():
            conn.execute(
                "INSERT INTO user_config (kind, value) VALUES (?, ?)",
                ("company_holds", json.dumps([{"company": "Example Co", "until": value}])),
            )
    finally:
        conn.close()
    return "/config/company_holds"


def _a_hold_file_quoting(root: Path, value: str) -> str:
    """The holds CSV the file fallback reads, with `value` as a hold date,
    which `ConfigError` quotes with `!r`."""
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "companies-hold.csv").write_text(
        f"company,reason,hold_until,notes\nExample Co,cooldown,{value},\n", encoding="utf-8"
    )
    return "/config/company_holds"


def _a_schedule_quoting(root: Path, value: str) -> str:
    """The schedule definition, with `value` as a job's name and no command.
    A name holding a path separator is refused and quoted with `!r`; any
    other is quoted plain in the missing command's refusal."""
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "schedule.json").write_text(
        json.dumps({"jobs": [{"name": value}]}), encoding="utf-8"
    )
    return "/ops/schedule"


ERROR_FIELDS: dict[str, Callable[[Path, str], str]] = {
    "ConfigOut.error from a stored row": _a_stored_hold_quoting,
    "ConfigOut.error from a config file": _a_hold_file_quoting,
    "ScheduleOut.error": _a_schedule_quoting,
}


def _error_field(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    plant: Callable[[Path, str], str],
    value: str,
    password: StorePassword,
) -> tuple[str, str]:
    """`value` planted where the route reads it, and the route driven as a
    client drives it, with the store URL configured: the field and the body."""
    monkeypatch.setenv("HARRIER_DATA_DIR", str(root / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    monkeypatch.chdir(root)
    monkeypatch.setattr("harrier.paths.repo_root", lambda: root)
    client = TestClient(create_app())
    path = plant(root, value)
    _point_at_postgres(monkeypatch, password)
    response = client.get(path, headers=auth())
    assert response.status_code == 200
    return response.json()["error"], response.text


@pytest.mark.parametrize("shape", ["the URL", "the password alone"])
@pytest.mark.parametrize("plant", list(ERROR_FIELDS.values()), ids=list(ERROR_FIELDS))
def test_an_error_field_in_a_200_body_has_the_store_password_masked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    store_password: StorePassword,
    plant: Callable[[Path, str], str],
    shape: str,
) -> None:
    """A refusal described in a 200 body never reaches the `HTTPException`
    handler, so each route scrubs its own `error` field (spec 133, Amendment
    (2026-10-12): exception text in a 200 body).

    The password alone, as written in the URL, is a shape `scrub_secrets`
    does not know: only the configured URL's forms mask it, so that case
    fails if a route scrubs with `scrub_secrets` alone or the scrub does not
    read `HARRIER_DATABASE_URL`."""
    value = store_password.url if shape == "the URL" else store_password.encoded
    error, body = _error_field(monkeypatch, tmp_path, plant, value, store_password)

    # The value was quoted and masked, not dropped.
    if shape == "the URL":
        assert "postgresql://harrier:REDACTED@db.example.test" in error
    else:
        assert "REDACTED" in error
    # The field as decoded, and the body as sent, since JSON escapes.
    assert store_password.leaks(error + body) == []


NOTHING_TO_MASK: dict[str, tuple[Callable[[Path, str], str], str, str]] = {
    "ConfigOut.error from a stored row": (
        _a_stored_hold_quoting,
        "soon",
        "company_holds entry {'company': 'Example Co', 'until': 'soon'} has unknown keys"
        " ['until']; expected only ['company', 'hold_until']",
    ),
    "ConfigOut.error from a config file": (
        _a_hold_file_quoting,
        "soon",
        "config/companies-hold.csv: hold for 'Example Co' has a malformed hold_until 'soon';"
        " expected YYYY-MM-DD",
    ),
    "ScheduleOut.error": (_a_schedule_quoting, "nightly", "jobs[0] (nightly): missing command"),
}


@pytest.mark.parametrize("case", list(NOTHING_TO_MASK.values()), ids=list(NOTHING_TO_MASK))
def test_an_error_field_with_nothing_to_mask_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    store_password: StorePassword,
    case: tuple[Callable[[Path, str], str], str, str],
) -> None:
    """The scrub runs with the store URL configured and changes nothing it
    does not mask: each field reads as the parser wrote it."""
    plant, value, expected = case
    error, _body = _error_field(monkeypatch, tmp_path, plant, value, store_password)
    assert error == expected
