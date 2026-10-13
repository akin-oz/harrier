"""Hosted logs carry no personal data (spec 135).

Two gaps ran once spec 134 let the hosted API serve. The hosted start
configured logging as the local one does, which opened a local SQLite
tracker under the data directory and a log file beside it. And a statement
Postgres refused, on a path no route catches, was logged by uvicorn with
the driver's error chained under the store error: `str()` of a psycopg
error carries the server's `DETAIL` lines, which quote the row.

Each test drives the decision, not the helper: the start runs as uvicorn
runs it, the failed statement goes through a hosted request, and the
records are read from the handlers `configure_logging` and uvicorn's own
configuration installed.
"""

# Pyright strict cannot resolve starlette's TestClient request and response
# members, which is why every API test file carries these.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import io
import json
import logging
import logging.config
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import psycopg
import pytest
from pg_support import open_owner_store
from test_hosted_auth import (
    API_HOST,
    CREDENTIALS_KEY_TEXT,
    SPA_ORIGIN,
    SUPABASE_URL,
    child_environment,
    start_api,
)
from test_hosted_requests import (
    PROBE_ROWS,
    Server,
    copied_server,
    create_probes,
    hosted_environment,
    keys_served_here,
    serving,
)
from test_hosted_start import start_as_a_process

import harrier.connection
import harrier.logredact
from harrier import logsetup
from harrier.connection import URL_VARIABLE, Connection, StoreDatabaseError, StoreIntegrityError
from harrier.db import connect
from harrier.llm.config import DEBUG_VARIABLES
from harrier.logredact import IdentityRedactionFilter
from harrier.logsetup import HOSTED_NOTE, SECRET_SCRUB, UVICORN_LOGGERS, configure_logging
from harrier.outreach.discovery import candidates_output_path
from harrier.tracker.store import add_job
from harrier.tracks import default_scope
from harrier_api.app import create_app
from harrier_api.hostedauth import HostedConfigError, prompt_logging_refusal

UVICORN_ERROR = "uvicorn.error"


@pytest.fixture(name="server")
def server_fixture(postgres_template: str) -> Iterator[Server]:
    yield from copied_server(postgres_template)


@pytest.fixture
def fresh_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Logging as a new process finds it, put back afterwards.

    `harrier_api.app` builds its app at import, so this session configured
    logging, locally, before any test ran; `configure_logging` would do
    nothing again. A hosted process configures it once, hosted.
    """
    root = logging.getLogger()
    saved_root = (list(root.handlers), root.level)
    saved_uvicorn = {
        name: (list(logger.handlers), logger.level, logger.propagate)
        for name in (*UVICORN_LOGGERS, UVICORN_ERROR)
        for logger in [logging.getLogger(name)]
    }
    installed = list(harrier.logredact._INSTALLED)  # pyright: ignore[reportPrivateUsage]
    # A filter added to a handler stays on it after the handler is put back.
    filters = {
        handler: list(handler.filters)
        for logger in (root, *(logging.getLogger(name) for name in saved_uvicorn))
        for handler in logger.handlers
    }
    monkeypatch.setattr(logsetup, "_configured", False)
    try:
        yield
    finally:
        for handler in root.handlers:
            if handler not in saved_root[0]:
                handler.close()
        root.handlers[:] = saved_root[0]
        root.setLevel(saved_root[1])
        for name, (handlers, level, propagate) in saved_uvicorn.items():
            logger = logging.getLogger(name)
            logger.handlers[:] = handlers
            logger.setLevel(level)
            logger.propagate = propagate
        for handler, kept_filters in filters.items():
            handler.filters[:] = kept_filters
        harrier.logredact.forget_all()
        for kept in installed:
            harrier.logredact.register(kept)


def configured_streams() -> list[logging.StreamHandler[io.StringIO]]:
    """Every stream handler on the root and on uvicorn's loggers."""
    found: list[logging.StreamHandler[io.StringIO]] = []
    for logger in (logging.getLogger(), *(logging.getLogger(name) for name in UVICORN_LOGGERS)):
        found.extend(
            handler
            for handler in logger.handlers
            if isinstance(handler, logging.StreamHandler)
            and not isinstance(handler, logging.FileHandler)
        )
    return found


def into_buffer() -> io.StringIO:
    """Point every configured stream handler at one buffer, so what a test
    reads is what those handlers, with their filters and formatters, wrote."""
    buffer = io.StringIO()
    streams = configured_streams()
    assert streams, "no stream handler was configured"
    for handler in streams:
        handler.setStream(buffer)
    return buffer


# --- the hosted start ---


def test_a_hosted_start_opens_no_local_store(server: Server, tmp_path: Path) -> None:
    """Imported as uvicorn imports it, in hosted mode on a store that passes
    the start checks, with an empty data directory."""
    code, output, _ = start_as_a_process(server.login_role(), tmp_path)
    assert code == 0, output
    data = tmp_path / "data"
    left = {path.name for path in data.iterdir()} if data.exists() else set()
    assert not {"harrier.db", "harrier.db-wal", "harrier.db-shm", "logs"} & left, left
    assert HOSTED_NOTE in output


def test_hosted_logging_writes_stderr_only(
    server: Server,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fresh_logging: None,
) -> None:
    with serving(monkeypatch, server.login_role().url):
        handlers = logging.getLogger().handlers
        assert handlers
        assert not any(isinstance(handler, logging.FileHandler) for handler in handlers)
        for handler in handlers:
            assert SECRET_SCRUB in handler.filters
            assert not any(isinstance(each, IdentityRedactionFilter) for each in handler.filters)
        # None is left registered either: the tracker write path refreshes a
        # registered filter by reading identity values through the request's
        # connection.
        assert not harrier.logredact._INSTALLED  # pyright: ignore[reportPrivateUsage]
    assert capsys.readouterr().err.count(HOSTED_NOTE) == 1


# --- a driver error in a log record ---


def test_a_failed_statement_logs_none_of_its_row(
    server: Server, monkeypatch: pytest.MonkeyPatch, fresh_logging: None
) -> None:
    """A row the store refuses with a `DETAIL` that quotes it, on a route that
    does not catch the refusal: the request fails as any unhandled exception
    does, and uvicorn logs it with its traceback."""
    create_probes(server)
    server.migrator(
        f"ALTER TABLE {PROBE_ROWS} ADD CONSTRAINT harrier_probe_no_synthetic "
        "CHECK (body NOT LIKE 'synthetic-row-%')"
    )
    # uvicorn configures its loggers before it imports the app, so the app
    # finds their handlers when it is built.
    from uvicorn.config import LOGGING_CONFIG

    logging.config.dictConfig(LOGGING_CONFIG)
    body = f"synthetic-row-{uuid.uuid4().hex}"
    with serving(monkeypatch, server.login_role().url) as hosted:
        buffer = into_buffer()
        logging.getLogger().setLevel(logging.DEBUG)
        with pytest.raises(StoreIntegrityError) as raised:
            hosted.client().post(
                "/probe/write",
                params={"body": body, "then": "none"},
                headers=hosted.headers(server.owner()),
            )
        # As uvicorn's protocol logs an exception the app let out
        # (`uvicorn/protocols/http/h11_impl.py`, `run_asgi`).
        logging.getLogger(UVICORN_ERROR).error(
            "Exception in ASGI application\n", exc_info=raised.value
        )
    written = buffer.getvalue()
    assert "Exception in ASGI application" in written
    assert body not in written
    assert "psycopg.errors.CheckViolation: [23514] new row for relation" in written
    # The route's frame is still printed.
    assert ", in write\n" in written


def refused_row(server: Server, body: str) -> StoreIntegrityError:
    """A store error raised from a psycopg error whose `DETAIL` quotes `body`."""
    raw = psycopg.connect(server.url, autocommit=True)
    raw.execute(b"CREATE TEMP TABLE refused (body text CHECK (body NOT LIKE 'synthetic-row-%'))")
    conn = Connection.postgres(raw)
    try:
        conn.execute("INSERT INTO refused (body) VALUES (?)", (body,))
    except StoreIntegrityError as error:
        assert body in str(error.__cause__), "the driver's text no longer quotes the row"
        return error
    finally:
        raw.close()
    raise AssertionError("the row was not refused")


def logged_through_configured_handlers(log: Callable[[logging.Logger], None]) -> str:
    """Configure logging as the hosted API does and run `log` on a domain
    logger, which propagates to the root's handlers."""
    configure_logging(force=True, hosted=True)
    buffer = into_buffer()
    log(logging.getLogger("harrier.tracker.store"))
    return buffer.getvalue()


def logged_with_exception(error: StoreIntegrityError) -> Callable[[logging.Logger], None]:
    def log(logger: logging.Logger) -> None:
        try:
            raise error
        except StoreIntegrityError:
            logger.exception("the write failed")

    return log


def logged_in_context(error: StoreIntegrityError) -> Callable[[logging.Logger], None]:
    def log(logger: logging.Logger) -> None:
        driver = error.__cause__
        assert isinstance(driver, psycopg.Error)
        try:
            try:
                raise driver
            except psycopg.Error:
                # Raised while handling it: the driver error is this one's
                # `__context__`, with no store error around it.
                raise RuntimeError("cleaning up failed")  # noqa: B904
        except RuntimeError:
            logger.exception("cleanup")

    return log


def logged_in_a_group(error: StoreIntegrityError) -> Callable[[logging.Logger], None]:
    def log(logger: logging.Logger) -> None:
        try:
            raise ExceptionGroup("the task group failed", [error])
        except ExceptionGroup:
            logger.exception("a task failed")

    return log


def logged_as_argument(error: StoreIntegrityError) -> Callable[[logging.Logger], None]:
    def log(logger: logging.Logger) -> None:
        logger.warning("the write failed: %s", error.__cause__)

    return log


def logged_as_mapping_argument(error: StoreIntegrityError) -> Callable[[logging.Logger], None]:
    def log(logger: logging.Logger) -> None:
        logger.warning("the write failed: %(error)s", {"error": error.__cause__})

    return log


def logged_as_message(error: StoreIntegrityError) -> Callable[[logging.Logger], None]:
    def log(logger: logging.Logger) -> None:
        logger.error(error.__cause__)

    return log


SHAPES: dict[str, Callable[[StoreIntegrityError], Callable[[logging.Logger], None]]] = {
    "logger.exception": logged_with_exception,
    "in a __context__": logged_in_context,
    "in an ExceptionGroup": logged_in_a_group,
    "as an argument": logged_as_argument,
    "as a mapping argument": logged_as_mapping_argument,
    "as the message": logged_as_message,
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_every_shape_of_driver_error_is_withheld(
    shape: str, server: Server, fresh_logging: None
) -> None:
    body = f"synthetic-row-{uuid.uuid4().hex}"
    error = refused_row(server, body)
    written = logged_through_configured_handlers(SHAPES[shape](error))
    assert written.strip()
    assert body not in written
    assert "[23514] new row for relation" in written


def test_a_sqlite_driver_error_is_withheld_too(fresh_logging: None) -> None:
    path = f"synthetic-path-{uuid.uuid4().hex}"
    conn = connect()
    try:
        with pytest.raises(StoreDatabaseError) as raised:
            conn.execute("SELECT json_extract('{}', ?)", (path,))
    finally:
        conn.close()
    assert path in str(raised.value.__cause__), "SQLite no longer quotes the path"
    error = raised.value

    def log(logger: logging.Logger) -> None:
        try:
            raise error
        except StoreDatabaseError:
            logger.exception("the read failed")

    written = logged_through_configured_handlers(log)
    assert path not in written
    assert "sqlite3.OperationalError: bad JSON path" in written


def test_an_unrenderable_driver_error_keeps_its_record(
    monkeypatch: pytest.MonkeyPatch, fresh_logging: None
) -> None:
    body = f"synthetic-row-{uuid.uuid4().hex}"
    driver = psycopg.errors.CheckViolation(
        f'new row for relation "jobs" violates check constraint "x"\n'
        f"DETAIL:  Failing row contains ({body})."
    )

    def cannot_render(_error: psycopg.Error) -> StoreDatabaseError:
        raise RuntimeError("the seam cannot render this error")

    monkeypatch.setattr(harrier.connection, "store_error_from_postgres", cannot_render)

    def log(logger: logging.Logger) -> None:
        try:
            raise StoreIntegrityError("check", "refused") from driver
        except StoreIntegrityError:
            logger.exception("the write failed")
        logger.warning("again: %s", driver)

    written = logged_through_configured_handlers(log)
    assert body not in written
    assert "log record withheld" not in written
    assert "the write failed" in written
    assert "\npsycopg.errors.CheckViolation\n" in written
    assert "again: psycopg.errors.CheckViolation" in written


# --- identity values hosted ---

COMPANY = "Northwind Labs"
TITLE = "Senior Frontend Engineer"


def stage_candidate(marker: str) -> str:
    """One invented candidate, written where discovery writes them, whose
    name and profile URL carry `marker`."""
    linkedin_url = f"https://www.linkedin.com/in/invented-{marker}"
    path = candidates_output_path(COMPANY, TITLE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "company": COMPANY,
                "role": TITLE,
                "candidates": [
                    {
                        "person_name": f"Avery Invented{marker}",
                        "person_title": "Engineering Manager",
                        "relevance": "hiring_manager",
                        "fit_score": "82",
                        "linkedin_url": linkedin_url,
                        "review_status": "pending",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return linkedin_url


def test_contact_routes_log_no_identity_value_hosted(
    server: Server, monkeypatch: pytest.MonkeyPatch, fresh_logging: None
) -> None:
    owner = server.owner()
    conn = open_owner_store(server.url, owner)
    try:
        job = add_job(
            conn,
            {
                "company": COMPANY,
                "title": TITLE,
                "url": "https://boards.example.com/northwind/1",
                "source": "greenhouse",
                "location": "Remote, Europe",
            },
            scope=default_scope(conn),
        )
    finally:
        conn.close()
    marker = uuid.uuid4().hex
    linkedin_url = stage_candidate(marker)
    with serving(monkeypatch, server.login_role().url) as hosted:
        buffer = into_buffer()
        logging.getLogger().setLevel(logging.DEBUG)
        client = hosted.client()
        headers = hosted.headers(owner)
        approved = client.post(
            f"/outreach/{job}/candidates/approve",
            json={"linkedin_url": linkedin_url},
            headers=headers,
        )
        assert approved.status_code == 200, approved.text
        best = client.post(
            f"/outreach/{job}/best-contact", json={"linkedin_url": linkedin_url}, headers=headers
        )
        assert best.status_code == 200, best.text
        listed = client.get("/outreach/contacts", headers=headers)
        assert listed.status_code == 200, listed.text
        # The routes read and wrote the contact: the marker reached them.
        assert marker in listed.text
    written = buffer.getvalue()
    assert written.strip(), "nothing was logged, so nothing was checked"
    assert marker not in written


# --- prompt logging and query strings (Amendment (2026-10-13)) ---


def test_a_hosted_start_refuses_prompt_logging(
    server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fresh_logging: None
) -> None:
    for name in DEBUG_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    url = server.login_role().url

    # As uvicorn starts it: the import fails and the process exits 1.
    environ = child_environment(
        tmp_path,
        HARRIER_AUTH="supabase",
        HARRIER_SUPABASE_URL=SUPABASE_URL,
        HARRIER_ALLOWED_HOSTS=API_HOST,
        HARRIER_CORS_ORIGINS=SPA_ORIGIN,
        HARRIER_DATABASE_URL=url,
        HARRIER_CREDENTIALS_KEY=CREDENTIALS_KEY_TEXT,
        CLAUDE_CLI_DEBUG=" Yes ",
    )
    started = start_api(environ, tmp_path)
    assert started.returncode == 1, started.stderr
    last_line = started.stderr.strip().splitlines()[-1]
    assert last_line == (
        f"harrier_api.hostedauth.HostedConfigError: {prompt_logging_refusal('CLAUDE_CLI_DEBUG')}"
    )
    assert "Yes" not in started.stderr

    keys_served_here(monkeypatch)
    hosted_environment(monkeypatch, url)
    for name in DEBUG_VARIABLES:
        for value in ("1", "true", "yes"):
            monkeypatch.setenv(name, value)
            with pytest.raises(HostedConfigError) as refused:
                create_app()
            assert str(refused.value) == prompt_logging_refusal(name)
            monkeypatch.delenv(name)
    # Two on: the first in the order the spec names is the one named.
    monkeypatch.setenv("ANTHROPIC_DEBUG", "1")
    monkeypatch.setenv("CODEX_CLI_DEBUG", "1")
    with pytest.raises(HostedConfigError) as refused:
        create_app()
    assert str(refused.value) == prompt_logging_refusal("CODEX_CLI_DEBUG")
    monkeypatch.delenv("ANTHROPIC_DEBUG")
    monkeypatch.delenv("CODEX_CLI_DEBUG")

    for name in DEBUG_VARIABLES:
        for value in ("0", ""):
            monkeypatch.setenv(name, value)
            with serving(monkeypatch, url):
                pass
            monkeypatch.delenv(name)

    # The local API starts with any of them on.
    monkeypatch.delenv("HARRIER_AUTH")
    monkeypatch.setenv(URL_VARIABLE, "")
    for name in DEBUG_VARIABLES:
        monkeypatch.setenv(name, "1")
        assert create_app() is not None
        monkeypatch.delenv(name)


def access_line(path: str, host: str) -> str:
    """Serve the app with uvicorn on a free port, as `uvicorn
    harrier_api.app:app` serves it, request `path`, and return what
    uvicorn's access handlers wrote."""
    import socket
    import threading
    import time
    import urllib.error
    import urllib.request

    import uvicorn

    listening = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listening.bind(("127.0.0.1", 0))
    port = int(listening.getsockname()[1])
    # Configured as uvicorn configures it: its logging first, then the app
    # is built, through the factory.
    server = uvicorn.Server(uvicorn.Config(create_app, factory=True, log_level="info"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listening]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "uvicorn did not start"
            time.sleep(0.02)
        buffer = io.StringIO()
        for handler in logging.getLogger("uvicorn.access").handlers:
            assert isinstance(handler, logging.StreamHandler)
            handler.setStream(buffer)
        request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"Host": host})
        try:
            with urllib.request.urlopen(request, timeout=10):
                pass
        except urllib.error.HTTPError:
            pass
    finally:
        server.should_exit = True
        thread.join(30)
        listening.close()
    return buffer.getvalue()


def test_a_hosted_access_line_carries_no_query(
    server: Server, monkeypatch: pytest.MonkeyPatch, fresh_logging: None
) -> None:
    query = f"synthetic-query-{uuid.uuid4().hex}"
    in_path = f"synthetic-path-{uuid.uuid4().hex}"

    local = access_line(f"/health?q={query}", "localhost")
    assert f"GET /health?q={query} HTTP/1.1" in local

    login = server.login_role()
    keys_served_here(monkeypatch)
    hosted_environment(monkeypatch, login.url)
    # The store password in the query as well: neither it nor the rest of
    # the query reaches the line.
    hosted = access_line(f"/health?q={query}&p={login.password}", API_HOST)
    assert '"GET /health HTTP/1.1" 200' in hosted
    assert query not in hosted
    assert login.password not in hosted
    assert "?" not in hosted

    # A `?` the path holds, encoded, is the path's and stays.
    encoded = access_line(f"/health%3F{in_path}?q={query}", API_HOST)
    assert f"GET /health%3F{in_path} HTTP/1.1" in encoded
    assert query not in encoded
