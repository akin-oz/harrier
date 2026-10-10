"""`harrier store`, and the refusals while HARRIER_DATABASE_URL names Postgres (spec 103).

Every SQLite database is built under `tmp_path` by the autouse data directory
fixture. The Postgres tests use a throwaway database from `pg_support` and
skip locally without a test server. Every URL and password here is synthetic.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from pg_support import fresh_database

import harrier.logsetup as logsetup
from harrier.db import default_db_path
from harrier.pgstore import URL_VARIABLE, StoreUrlError
from harrier.tracker.schema import MIGRATIONS
from harrier_api.app import create_app
from harrier_cli.main import main

SPEC_112_REFUSAL = (
    "error: HARRIER_DATABASE_URL names a Postgres store; only harrier store runs on it "
    "until spec 112\n"
)
SCHEME_REFUSAL = "error: HARRIER_DATABASE_URL must be empty or a postgresql:// URL\n"
# Nothing listens on port 1, so a connection is refused at once.
UNREACHABLE_URL = "postgresql://someone@127.0.0.1:1/nowhere"
SQLITE_LATEST = max(version for version, _ in MIGRATIONS)


def run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture()
def pg_url() -> Iterator[str]:
    """An empty Postgres database, dropped afterwards."""
    with fresh_database() as url:
        yield url


@pytest.fixture()
def sqlite_store(monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv(URL_VARIABLE, raising=False)
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    # Logging is set up once per process, and importing the API already did
    # it. Reset, so a command that set it up again would open, and migrate,
    # the file here as it does in a fresh process.
    monkeypatch.setattr(logsetup, "_configured", False)
    return default_db_path()


def test_store_migrate_prints_before_and_after_on_postgres(
    pg_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(URL_VARIABLE, pg_url)

    assert run(["store", "migrate"], capsys) == (0, "postgres 0 -> 9\n", "")
    assert run(["store", "migrate"], capsys) == (0, "postgres 9 -> 9\n", "")
    assert run(["store", "status"], capsys) == (0, "postgres 9\n", "")
    # The Postgres path never touches the local file.
    assert not default_db_path().exists()


def test_store_status_reports_behind_on_an_unmigrated_store(
    pg_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(URL_VARIABLE, pg_url)

    assert run(["store", "status"], capsys) == (0, "postgres 0\nbehind: 9 expected\n", "")


def test_store_refuses_a_postgres_store_ahead_of_the_code(
    pg_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(URL_VARIABLE, pg_url)
    assert run(["store", "migrate"], capsys)[0] == 0
    import psycopg

    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("INSERT INTO schema_version (version) VALUES (12)")

    for command in ("status", "migrate"):
        code, out, err = run(["store", command], capsys)
        assert (code, out) == (1, ""), command
        assert err.startswith("error: "), command
        assert "version 12" in err and "(9)" in err, command


def test_store_commands_on_sqlite(sqlite_store: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Status reads without writing: no store yet, and still none afterwards.
    assert run(["store", "status"], capsys) == (0, "sqlite 0\n", "")
    assert not sqlite_store.exists()

    # The version before is read before the open applies the migrations.
    assert run(["store", "migrate"], capsys) == (0, f"sqlite 0 -> {SQLITE_LATEST}\n", "")
    assert run(["store", "migrate"], capsys) == (
        0,
        f"sqlite {SQLITE_LATEST} -> {SQLITE_LATEST}\n",
        "",
    )
    assert run(["store", "status"], capsys) == (0, f"sqlite {SQLITE_LATEST}\n", "")


def test_store_refuses_a_sqlite_file_ahead_of_the_code(
    sqlite_store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["store", "migrate"]) == 0
    ahead = SQLITE_LATEST + 3
    with closing(sqlite3.connect(sqlite_store)) as conn, conn:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (ahead,))
    capsys.readouterr()

    for command in ("status", "migrate"):
        code, out, err = run(["store", command], capsys)
        assert (code, out) == (1, ""), command
        assert err.startswith("error: "), command
        assert f"version {ahead}" in err and f"({SQLITE_LATEST})" in err, command


def test_an_unsupported_url_is_refused_before_anything_opens(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for url in ("mysql://x", "/a/bare/path.db"):
        monkeypatch.setenv(URL_VARIABLE, url)
        for argv in (["tracks", "list"], ["store", "status"], ["doctor"]):
            assert run(argv, capsys) == (1, "", SCHEME_REFUSAL), (url, argv)
    assert not default_db_path().exists()
    assert not default_db_path().parent.exists()


def test_a_data_command_is_refused_on_a_postgres_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(URL_VARIABLE, UNREACHABLE_URL)

    for argv in (["tracks", "list"], ["--track", "default", "next"], ["doctor"]):
        assert run(argv, capsys) == (1, "", SPEC_112_REFUSAL), argv
    assert not default_db_path().exists()
    assert not default_db_path().parent.exists()


def test_store_on_an_unreachable_postgres_never_prints_the_password(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Made per run: distinctive enough to find, and no literal in the source
    # for a secret scanner to read as a credential.
    password = f"synthetic{uuid.uuid4().hex}"
    monkeypatch.setenv(URL_VARIABLE, f"postgresql://someone:{password}@127.0.0.1:1/nowhere")
    caplog.set_level(logging.DEBUG)

    for command in ("status", "migrate"):
        code, out, err = run(["store", command], capsys)
        assert (code, out) == (1, ""), command
        assert err.startswith("error: "), command
        assert password not in err, command
        assert "postgresql://" not in err, command
    assert password not in caplog.text
    assert not default_db_path().exists()


def test_the_api_refuses_to_start_on_a_postgres_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(URL_VARIABLE, UNREACHABLE_URL)
    with pytest.raises(RuntimeError) as refused:
        create_app()
    assert str(refused.value) == SPEC_112_REFUSAL.removeprefix("error: ").rstrip("\n")

    monkeypatch.setenv(URL_VARIABLE, "mysql://x")
    with pytest.raises(StoreUrlError) as invalid:
        create_app()
    assert f"error: {invalid.value}\n" == SCHEME_REFUSAL
    assert not default_db_path().exists()
