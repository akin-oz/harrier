"""`harrier store`, and the refusals while HARRIER_DATABASE_URL names Postgres (spec 103).

Every SQLite database is built under `tmp_path` by the autouse data directory
fixture. The Postgres tests use a throwaway database from `pg_support` and
skip locally without a test server. Every URL and password here is synthetic.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import uuid
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from pg_support import fresh_database

import harrier.logsetup as logsetup
from harrier.connection import URL_VARIABLE, StoreUrlError, store_target
from harrier.db import default_db_path
from harrier.tracker import schema
from harrier.tracker.schema import MIGRATIONS, POSTGRES_MIGRATIONS
from harrier_api.app import create_app
from harrier_cli.main import load_project_env, main

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
    # Empty, not unset: unset, `.env` in pytest's working directory would
    # choose the store (review of PR #218).
    monkeypatch.setenv(URL_VARIABLE, "")
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

    # The newest migration, whichever spec added it (105, then 099).
    assert run(["store", "migrate"], capsys) == (0, f"postgres 0 -> {POSTGRES_LATEST}\n", "")
    latest = f"postgres {POSTGRES_LATEST} -> {POSTGRES_LATEST}\n"
    assert run(["store", "migrate"], capsys) == (0, latest, "")
    assert run(["store", "status"], capsys) == (0, f"postgres {POSTGRES_LATEST}\n", "")
    # The Postgres path never touches the local file.
    assert not default_db_path().exists()


def test_a_failed_migration_exits_1_with_the_drivers_message(
    pg_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A statement the server refuses escaped `harrier store` as a driver
    traceback (post-merge review of PR #207). It is one error line now,
    quoting the server's message and never the URL."""
    real = list(schema.POSTGRES_MIGRATIONS)
    broken = real[-1][0] + 1
    monkeypatch.setattr(schema, "POSTGRES_MIGRATIONS", [*real, (broken, ["THIS IS NOT SQL"])])
    monkeypatch.setenv(URL_VARIABLE, pg_url)

    code, out, err = run(["store", "migrate"], capsys)

    assert (code, out) == (1, "")
    assert err == 'error: a migration failed: syntax error at or near "THIS"\n'
    # The baseline before it committed; the broken migration left no row.
    assert run(["store", "status"], capsys) == (
        0,
        f"postgres {real[-1][0]}\nbehind: {broken} expected\n",
        "",
    )


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

    # One past the newest version this code knows, whatever that is.
    ahead = POSTGRES_LATEST + 1

    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(f"INSERT INTO schema_version (version) VALUES ({ahead})".encode())

    for command in ("status", "migrate"):
        code, out, err = run(["store", command], capsys)
        assert (code, out) == (1, ""), command
        assert err.startswith("error: "), command
        assert f"version {ahead}" in err and f"({POSTGRES_LATEST})" in err, command


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


@pytest.mark.dotenv_fallback
def test_a_url_in_dotenv_reaches_the_api_as_it_reaches_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI loads `.env` and the API did not, so a URL set only there made
    the CLI refuse while the API served SQLite: the mixed-store case spec 103
    exists to prevent (post-merge review of PR #207)."""
    # Into tmp_path before the variable goes, so no other `.env` is read.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(URL_VARIABLE)
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


@pytest.mark.parametrize("command", ["status", "migrate"])
def test_a_postgres_url_without_the_driver_exits_1_naming_the_install(
    command: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The refusal spec 103 names for a missing driver, through the CLI and
    its exit status, not only the function (post-merge review of PR #207).
    `migrate` checks for the driver on its own path before it imports
    psycopg, so it is run too (review of PR #218)."""
    monkeypatch.setitem(sys.modules, "psycopg", None)
    monkeypatch.setenv(URL_VARIABLE, UNREACHABLE_URL)
    code, _, err = run(["store", command], capsys)
    assert code == 1
    assert err.startswith("error: ")
    assert "uv sync --group postgres" in err


def test_a_postgres_url_in_dotenv_never_reaches_a_sqlite_test(
    sqlite_store: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The `sqlite_store` fixture deleted the variable conftest pins empty, so
    `.env` in pytest's working directory chose the store, and SQLite tests
    ran `harrier store migrate` against the database it named (review of PR
    #218). The `.env` here names a closed port, so nothing is reached even
    when the pin is lost."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"{URL_VARIABLE}={UNREACHABLE_URL}\n", encoding="utf-8")

    assert run(["store", "status"], capsys) == (0, "sqlite 0\n", "")


def test_the_first_line_for_a_key_wins_in_the_cli_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI's loader never overwrote a key, including one it had just set
    from an earlier line, so the first line won. The shared parser made the
    last one win, a change no spec recorded (review of PR #218)."""
    name = "HARRIER_TEST_DUPLICATED_KEY"
    # Set, then deleted, so the teardown removes what the loader writes.
    monkeypatch.setenv(name, "")
    monkeypatch.delenv(name)
    env_file = tmp_path / ".env"
    env_file.write_text(f"{name}=first\n{name}=second\n", encoding="utf-8")

    load_project_env(env_file)

    assert os.environ[name] == "first"


@pytest.mark.dotenv_fallback
def test_the_first_line_for_the_url_wins_in_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same rule for the store, read by the API through `store_target` and
    by the CLI through its loader: an empty first line keeps SQLite."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(URL_VARIABLE)
    (tmp_path / ".env").write_text(
        f"{URL_VARIABLE}=\n{URL_VARIABLE}={UNREACHABLE_URL}\n", encoding="utf-8"
    )

    assert store_target().dialect == "sqlite"
    assert run(["store", "status"], capsys) == (0, "sqlite 0\n", "")


# What the net in conftest.py runs against: a test that drops the pin and
# calls main(), whose `.env` loader writes the URL back, so the variable is
# set again by the time the test ends. Run in its own pytest process, with
# this suite's conftest.py.
PIN_DROPPING_TESTS = f'''
import os
import subprocess
import sys

import pytest

from harrier_cli.main import main


def test_drops_the_pin_and_lets_dotenv_choose(monkeypatch):
    monkeypatch.delenv("{URL_VARIABLE}")
    assert main(["tracks", "list"]) == 1
    assert os.environ["{URL_VARIABLE}"] == "{UNREACHABLE_URL}"


@pytest.mark.dotenv_fallback
def test_drops_the_pin_inside_its_tmp_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("{URL_VARIABLE}")
    assert main(["tracks", "list"]) == 0


@pytest.mark.dotenv_fallback
def test_drops_the_pin_outside_its_tmp_path(monkeypatch):
    monkeypatch.delenv("{URL_VARIABLE}")


@pytest.fixture(scope="module")
def drops_the_pin_for_the_module():
    os.environ.pop("{URL_VARIABLE}")
    yield


def test_uses_a_module_fixture_that_drops_the_pin(drops_the_pin_for_the_module):
    assert os.environ["{URL_VARIABLE}"] == ""


def test_deletes_the_pin_without_an_event():
    del os.environ._data[os.environ.encodekey("{URL_VARIABLE}")]


def test_deletes_the_pin_without_an_event_then_lets_dotenv_choose():
    del os.environ._data[os.environ.encodekey("{URL_VARIABLE}")]
    assert main(["tracks", "list"]) == 1


def test_replaces_the_environment_without_the_pin(monkeypatch):
    monkeypatch.setattr(
        os, "environ", {{k: v for k, v in os.environ.items() if k != "{URL_VARIABLE}"}}
    )


def test_starts_a_child_without_the_pin():
    env = {{k: v for k, v in os.environ.items() if k != "{URL_VARIABLE}"}}
    subprocess.run([sys.executable, "-c", "pass"], env=env, check=True)


def test_leaves_the_pin_alone():
    assert os.environ["{URL_VARIABLE}"] == ""
'''


def test_the_pin_net_fails_a_test_that_dropped_the_pin_whatever_it_holds_after(
    pytester: pytest.Pytester,
) -> None:
    """The net checked the variable only at the end of a test, and only for
    absence. A test that deleted the pin and then called main() let `.env`
    write the variable back, so the net said nothing (review of PR #225).
    It now records every unset as it happens. None of the tests below asks
    for the net, so a failure also proves it is autouse.

    A marked test that unsets the pin outside its tmp_path had no case, and
    an unset before a test's own watch began, as in a module-scoped fixture,
    went unseen (post-merge review of PR #231). Every unset is now recorded
    with the phase it happened in, and one outside every test's watch fails
    the session.

    A pin lost with no unset event (deleted from `os.environ._data`, or
    `os.environ` replaced), and a child started without it, went unseen
    too (privacy review and claims audit of PR #243). The end of each test
    checks the pin is present and unchanged, the `.env` loader writing it
    counts as losing it, and so does a child's environment without it."""
    pytester.makeconftest(Path(__file__).with_name("conftest.py").read_text(encoding="utf-8"))
    pytester.makepyfile(test_pin=PIN_DROPPING_TESTS)
    (pytester.path / ".env").write_text(f"{URL_VARIABLE}={UNREACHABLE_URL}\n", encoding="utf-8")

    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")

    # Every test passes. Seven errors: the unmarked test that drops the pin,
    # the marked one that drops it outside its tmp_path, and the four that
    # lose it by a path with no unset event, each fail at their own
    # teardown; the module fixture's unset fails the session, at the
    # teardown of the last test.
    result.assert_outcomes(passed=9, errors=7)
    result.stdout.fnmatch_lines(
        [
            "*ERROR at teardown of test_drops_the_pin_and_lets_dotenv_choose*",
            "*test_drops_the_pin_and_lets_dotenv_choose unset HARRIER_DATABASE_URL*",
            "*ERROR at teardown of test_drops_the_pin_outside_its_tmp_path*",
            "*test_drops_the_pin_outside_its_tmp_path unset HARRIER_DATABASE_URL outside its "
            "tmp_path*",
            "*test_deletes_the_pin_without_an_event removed HARRIER_DATABASE_URL without an "
            "unset event*",
            "*test_deletes_the_pin_without_an_event_then_lets_dotenv_choose let the .env loader "
            "write HARRIER_DATABASE_URL*",
            "*test_replaces_the_environment_without_the_pin removed HARRIER_DATABASE_URL without "
            "an unset event*",
            "*test_starts_a_child_without_the_pin started a child without HARRIER_DATABASE_URL*",
            "*ERROR at teardown of test_leaves_the_pin_alone*",
            "*HARRIER_DATABASE_URL was unset outside every test's watch, during setup of "
            "test_pin.py::test_uses_a_module_fixture_that_drops_the_pin*",
        ]
    )
