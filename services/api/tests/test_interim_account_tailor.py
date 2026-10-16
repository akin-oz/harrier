"""`tailor` and `cover-letter` run against a hosted account, interim (spec 147).

Each test runs on its own copy of the migrated Postgres template
(`postgres_template`, spec 126), with a run login made for it, and drives
`harrier --owner <user id> tailor|cover-letter --job-id <job>` through `main`.
The AI step is off (`--no-ai`) or stubbed, and Chromium is a stand-in: the
render and gate code are the real ones. Every company, posting and document
is synthetic. The tests that need Postgres skip locally without a test server
and fail in CI (spec 039).
"""

# Pyright strict cannot resolve psycopg's row types.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false, reportPrivateUsage=false

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pg_support import open_owner_store
from resume_support import REPO_ROOT, example_documents, store_documents
from test_academic_applications import _no_browser
from test_account_writes import Account, as_account, seed_resume, truth_lines
from test_apply_claims import (
    CHECKOUT,
    COMPANY,
    PARAGRAPH_THREE,
    PROFILE_JSON_PATH,
    PROFILE_MD_PATH,
    ROLE,
    TRUTH,
    letter_json,
    stub_letter,
)
from test_hosted_requests import Server, copied_server, run_login_for
from test_import_local import hosted_query

import harrier.logsetup as logsetup
import harrier_cli.main as cli
from harrier.accountstore import ACCOUNT_COMMANDS, ACCOUNT_URL_VARIABLE
from harrier.connection import Connection
from harrier.db import connect, default_db_path
from harrier.profile.store import put_document
from harrier.tracker.actions import add_manually
from harrier.tracks import default_scope
from harrier_cli.main import main

POSTING = (
    "Examplesoft builds invoicing tools for small firms. "
    "The team ships to customers every week. "
    "We enforce code review on every change."
)
URL = "https://jobs.example.test/interim-147"
CANDIDATE = "Deniz Örnek"

# Spec 144's commands, written out here rather than read from the code.
SPEC_144_COMMANDS = {
    "tracks add",
    "tracks list",
    "profile put",
    "profile list",
    "profile check",
    "move",
}


@pytest.fixture(name="server")
def server_fixture(postgres_template: str) -> Iterator[Server]:
    yield from copied_server(postgres_template)


@pytest.fixture()
def account(server: Server) -> Iterator[Account]:
    with run_login_for(server.url) as login:
        yield Account(server, login, server.owner())


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data directory of the test's own, no account URL unless a test
    exports one, and nothing bound to Storage. The working directory is the
    repository's, where the resume template is read from."""
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    monkeypatch.delenv("HARRIER_AUTH", raising=False)
    monkeypatch.delenv(ACCOUNT_URL_VARIABLE, raising=False)
    monkeypatch.setenv("HARRIER_DATABASE_URL", "")
    monkeypatch.chdir(REPO_ROOT)
    return tmp_path


def seed_letter(conn: Connection) -> None:
    """What a cover letter reads, on top of the resume documents: the
    candidate, the application profile, and a truth that holds the
    evidence the stubbed letter cites."""
    track = default_scope(conn).track.id
    put_document(
        conn,
        "candidate",
        "candidate.json",
        "json",
        json.dumps({"candidate": {"name": CANDIDATE, "location": "Exampleland"}}),
    )
    put_document(
        conn,
        "application_profile",
        "application-profile.json",
        "json",
        PROFILE_JSON_PATH.read_text(encoding="utf-8"),
        track_id=track,
    )
    put_document(
        conn,
        "application_profile",
        "application-profile.md",
        "markdown",
        PROFILE_MD_PATH.read_text(encoding="utf-8"),
        track_id=track,
    )
    # The pool first: TRUTH ends under its "Claims I must not make" heading.
    put_document(conn, "resume_truth", "truth.md", "markdown", "\n".join([*truth_lines(), TRUTH]))
    facts, framing = example_documents()
    facts["verified_skills"] = [*facts.get("verified_skills", []), "TypeScript", "React"]
    facts["all_skills"] = [*facts.get("all_skills", []), "TypeScript", "React"]
    store_documents(conn, facts, framing)


def add_job(conn: Connection) -> int:
    _, row = add_manually(
        conn, default_scope(conn), company=COMPANY, title=ROLE, url=URL, location="Remote, Europe"
    )
    assert row is not None
    return int(row["id"])


def seed_account_with_job(account: Account, *, letter: bool = False) -> int:
    conn = open_owner_store(account.server.url, account.owner)
    try:
        seed_resume(conn)
        if letter:
            seed_letter(conn)
        return add_job(conn)
    finally:
        conn.close()


def posting_file(directory: Path) -> str:
    path = directory / "posting.txt"
    path.write_text(POSTING, encoding="utf-8")
    return str(path)


def hosted_job(account: Account, job_id: int) -> dict[str, Any]:
    [row] = hosted_query(
        account.server,
        "SELECT status FROM jobs WHERE owner_id = %s AND id = %s",
        (account.owner, job_id),
    )
    return row


def hosted_events(account: Account, job_id: int) -> list[dict[str, Any]]:
    return hosted_query(
        account.server,
        "SELECT kind FROM job_events WHERE owner_id = %s AND job_id = %s ORDER BY id",
        (account.owner, job_id),
    )


def refuse_local_store(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Any open of a SQLite file, by the command's own `connect` or by any
    library under it, fails the test. Returns the opens the test allowed
    (none): the list stays empty while the test passes."""
    opened: list[str] = []

    def refuse(*_args: object, **_kwargs: object) -> object:
        opened.append("sqlite")
        raise AssertionError("an account command opened the local SQLite file")

    monkeypatch.setattr(cli, "connect", refuse)
    monkeypatch.setattr(sqlite3, "connect", refuse)
    return opened


# --- the tests the spec names ---


def test_tailor_with_owner_reads_and_records_in_the_account(
    account: Account,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = seed_account_with_job(account)
    posting = posting_file(tmp_path)
    _no_browser(monkeypatch, pages=1)
    opened = refuse_local_store(monkeypatch)
    before = hosted_events(account, job_id)
    assert hosted_job(account, job_id)["status"] == "prospect"

    ran = as_account(
        monkeypatch, capsys, account.login, account.owner,
        "tailor", "--job-id", str(job_id), "--jd-file", posting, "--no-ai",
    )  # fmt: skip

    assert ran.code == 0, ran.err
    out = dict(line.split("=", 1) for line in ran.out.splitlines())
    pdf = Path(out["tailored_pdf"])
    metadata = Path(out["metadata"])
    data = tmp_path / "data"
    # Files stay local, under the test's data directory.
    assert pdf.is_file()
    assert pdf.is_relative_to(data)
    assert metadata.is_file()
    # The job and the profile documents came from the account.
    meta = json.loads(metadata.read_text(encoding="utf-8"))
    assert (meta["company"], meta["requested_role"]) == (COMPANY, ROLE)
    facts, _ = example_documents()
    assert str(facts["candidate"]["name"]) in pdf.with_suffix(".md").read_text(encoding="utf-8")
    # The status change is in the account, with its event, and in no local store.
    assert hosted_job(account, job_id)["status"] == "tailored_cv_requested"
    assert len(hosted_events(account, job_id)) > len(before)
    assert opened == []
    assert not default_db_path().exists()


def test_cover_letter_with_owner_reads_and_records_in_the_account(
    account: Account,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_id = seed_account_with_job(account, letter=True)
    posting = posting_file(tmp_path)
    _no_browser(monkeypatch, pages=1)
    stub_letter(monkeypatch, letter_json(last=PARAGRAPH_THREE))
    opened = refuse_local_store(monkeypatch)
    status = hosted_job(account, job_id)["status"]
    before = hosted_events(account, job_id)

    ran = as_account(
        monkeypatch, capsys, account.login, account.owner,
        "cover-letter", "--job-id", str(job_id), "--jd-file", posting,
    )  # fmt: skip

    assert ran.code == 0, ran.err
    out = dict(line.split("=", 1) for line in ran.out.splitlines())
    pdf = Path(out["pdf"])
    # Files stay local, under the test's data directory.
    assert pdf.is_file()
    assert pdf.is_relative_to(tmp_path / "data")
    # The job came from the account (its company is in the letter) and so did
    # the candidate document (its name is in the letter's file).
    letter = Path(out["markdown"]).read_text(encoding="utf-8")
    assert CHECKOUT in letter
    assert CANDIDATE in Path(out["html"]).read_text(encoding="utf-8")
    # A letter changes no status, as locally: the account's row is as it was.
    assert hosted_job(account, job_id)["status"] == status
    assert hosted_events(account, job_id) == before
    assert opened == []
    assert not default_db_path().exists()


def test_tailor_and_cover_letter_without_owner_stay_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    local = connect()
    try:
        seed_resume(local)
        seed_letter(local)
        job_id = add_job(local)
    finally:
        local.close()
    capsys.readouterr()
    posting = posting_file(tmp_path)
    _no_browser(monkeypatch, pages=1)
    stub_letter(monkeypatch, letter_json(last=PARAGRAPH_THREE))
    opens: list[Connection] = []
    real_connect = cli.connect

    def counting_connect(*args: Any, **kwargs: Any) -> Connection:
        opened = real_connect(*args, **kwargs)
        opens.append(opened)
        return opened

    monkeypatch.setattr(cli, "connect", counting_connect)

    assert main(["tailor", "--job-id", str(job_id), "--jd-file", posting, "--no-ai"]) == 0
    assert len(opens) == 1
    assert main(["cover-letter", "--job-id", str(job_id), "--jd-file", posting]) == 0
    assert len(opens) == 2
    capsys.readouterr()

    local = connect()
    try:
        [row] = local.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchall()
    finally:
        local.close()
    assert row[0] == "tailored_cv_requested"


def test_tailor_with_owner_never_opens_the_local_store(
    account: Account,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With `connect` and SQLite's own `connect` refused for the whole run,
    each command still succeeds and no local database file appears. Logging
    setup is the usual hidden opener: spec 144 skips it for `--owner`."""
    job_id = seed_account_with_job(account, letter=True)
    posting = posting_file(tmp_path)
    _no_browser(monkeypatch, pages=1)
    stub_letter(monkeypatch, letter_json(last=PARAGRAPH_THREE))
    refuse_local_store(monkeypatch)
    # Collecting the API's tests configured logging already, which makes
    # `configure_logging` return at once. Unset, a command that ran it would
    # open the local store to load its redaction values, as it does locally.
    monkeypatch.setattr(logsetup, "_configured", False)

    tailored = as_account(
        monkeypatch, capsys, account.login, account.owner,
        "tailor", "--job-id", str(job_id), "--jd-file", posting, "--no-ai",
    )  # fmt: skip
    lettered = as_account(
        monkeypatch, capsys, account.login, account.owner,
        "cover-letter", "--job-id", str(job_id), "--jd-file", posting,
    )  # fmt: skip

    assert (tailored.code, lettered.code) == (0, 0), tailored.err + lettered.err
    assert not default_db_path().exists()
    assert not list((tmp_path / "data").glob("*.db*"))


def test_account_commands_hold_spec_144_plus_tailor_and_cover_letter() -> None:
    assert SPEC_144_COMMANDS | {"tailor", "cover-letter"} == ACCOUNT_COMMANDS
