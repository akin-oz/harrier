"""A throwaway Postgres database per test (spec 103).

The Postgres tests need a server. `HARRIER_TEST_POSTGRES_URL` names one
whose user may create databases; each test gets its own database, dropped
afterwards. Locally, without the URL, the tests skip and pytest's summary
says so. In CI (`CI=true`) a missing URL or an unreachable server fails the
run: a gate that skips in CI has not run (spec 039).

To run them locally:

    docker run -d --rm --name harrier-pg -e POSTGRES_PASSWORD=postgres \\
        -p 127.0.0.1:55432:5432 postgres:17
    HARRIER_TEST_POSTGRES_URL=postgresql://postgres:postgres@127.0.0.1:55432/postgres \\
        uv run pytest -q
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit

import pytest

TEST_URL_VARIABLE = "HARRIER_TEST_POSTGRES_URL"


def _in_ci() -> bool:
    return os.environ.get("CI", "").strip().lower() in {"1", "true", "yes"}


def admin_url() -> str:
    """The test server's URL, or skip (locally) or fail (in CI)."""
    url = os.environ.get(TEST_URL_VARIABLE, "").strip()
    if not url:
        message = f"{TEST_URL_VARIABLE} is not set; the Postgres tests need a server"
        if _in_ci():
            pytest.fail(message, pytrace=False)
        pytest.skip(message)
    try:
        import psycopg  # noqa: F401  # pyright: ignore[reportUnusedImport]
    except ImportError:
        message = "psycopg is not installed; run uv sync (the dev group includes postgres)"
        if _in_ci():
            pytest.fail(message, pytrace=False)
        pytest.skip(message)
    return url


def _with_database(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))


@contextmanager
def fresh_database() -> Iterator[str]:
    """Create an empty database, yield its URL, then drop it."""
    import psycopg

    admin = admin_url()
    name = f"harrier_test_{uuid.uuid4().hex[:12]}"
    try:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f"CREATE DATABASE {name}".encode())
    except psycopg.OperationalError as error:
        message = f"cannot reach the Postgres test server: {type(error).__name__}"
        if _in_ci():
            pytest.fail(message, pytrace=False)
        pytest.skip(message)
    try:
        yield _with_database(admin, name)
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)".encode())


@pytest.fixture
def postgres_url() -> Iterator[str]:
    """An empty Postgres database for one test."""
    with fresh_database() as url:
        yield url
