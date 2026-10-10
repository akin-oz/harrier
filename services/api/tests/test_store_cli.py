"""`harrier store`, and the refusals while HARRIER_DATABASE_URL names Postgres (spec 103).

Every SQLite database is built under `tmp_path` by the autouse data directory
fixture. The Postgres tests use a throwaway database from `pg_support` and
skip locally without a test server. Every URL and password here is synthetic.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
import uuid
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from pg_support import fresh_database

import harrier.logsetup as logsetup
from harrier.db import default_db_path
from harrier.pgstore import URL_VARIABLE, StoreUrlError
from harrier.tracker.schema import MIGRATIONS, POSTGRES_MIGRATIONS
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
POSTGRES_LATEST = max(version for version, _ in POSTGRES_MIGRATIONS)


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

    # Spec 105 names the numbers: the baseline (9) and migration 10.
    assert run(["store", "migrate"], capsys) == (0, "postgres 0 -> 10\n", "")
    assert run(["store", "migrate"], capsys) == (0, "postgres 10 -> 10\n", "")
    assert run(["store", "status"], capsys) == (0, "postgres 10\n", "")
    # The Postgres path never touches the local file.
    assert not default_db_path().exists()


def test_store_status_reports_behind_on_an_unmigrated_store(
    pg_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(URL_VARIABLE, pg_url)

    assert run(["store", "status"], capsys) == (
        0,
        f"postgres 0\nbehind: {POSTGRES_LATEST} expected\n",
        "",
    )


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
        assert "version 12" in err and f"({POSTGRES_LATEST})" in err, command


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


def test_a_url_in_dotenv_reaches_the_api_as_it_reaches_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI loads `.env` and the API did not, so a URL set only there made
    the CLI refuse while the API served SQLite: the mixed-store case spec 103
    exists to prevent (post-merge review of PR #207)."""
    monkeypatch.delenv(URL_VARIABLE, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"{URL_VARIABLE}={UNREACHABLE_URL}\n", encoding="utf-8")

    with pytest.raises(RuntimeError) as refused:
        create_app()
    assert str(refused.value) == SPEC_112_REFUSAL.removeprefix("error: ").rstrip("\n")

    code, _, err = run(["tracks", "list"], capsys)
    assert (code, err) == (1, SPEC_112_REFUSAL)
    assert not default_db_path().exists()


def test_an_exported_value_wins_over_dotenv_for_the_api_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exported variable, even empty, is never overridden by `.env`: the
    CLI's rule (spec 011), now the API's as well."""
    monkeypatch.setenv(URL_VARIABLE, "")
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"{URL_VARIABLE}={UNREACHABLE_URL}\n", encoding="utf-8")

    create_app()


def test_a_postgres_url_without_the_driver_exits_1_naming_the_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The refusal spec 103 names for a missing driver, through the CLI and
    its exit status, not only the function (post-merge review of PR #207)."""
    monkeypatch.setitem(sys.modules, "psycopg", None)
    monkeypatch.setenv(URL_VARIABLE, UNREACHABLE_URL)
    code, _, err = run(["store", "status"], capsys)
    assert code == 1
    assert err.startswith("error: ")
    assert "uv sync --group postgres" in err
