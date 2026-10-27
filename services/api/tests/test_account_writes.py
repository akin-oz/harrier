"""The operator writes one hosted account from the command line (spec 144).

Each hosted test runs on its own copy of the migrated Postgres template
(`postgres_template`, spec 126), with a run login made for it, and drives
`harrier --owner <user id> ...` through `main`. The local store, where a test
needs one, is under its own tmp_path. Every company, posting and document is
synthetic: the resume documents are the committed examples. The tests that
need Postgres skip locally without a test server and fail in CI (spec 039).
"""

# Pyright strict cannot resolve psycopg's row types.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, cast

import pytest
from pg_support import open_owner_store
from resume_support import REPO_ROOT, example_documents, store_documents
from test_hosted_requests import Server, copied_server, run_login_for
from test_import_local import hosted_query

import harrier_cli.main as cli
from harrier import container
from harrier.accountstore import ACCOUNT_URL_VARIABLE, NO_ACCOUNT, account_refusal
from harrier.connection import Connection
from harrier.db import connect
from harrier.profile.store import put_document
from harrier.tracker.actions import add_manually
from harrier.tracker.reasons import MOVED
from harrier.tracks import default_scope
from harrier_cli.main import main

CONFIG = REPO_ROOT / "config"
SCREENING = CONFIG / "screening.focused.example.json"
FOCUSED_FRAMING = CONFIG / "resume-framing.focused.example.json"
FOCUSED_PROFILE = CONFIG / "application-profile.focused.example.json"
SLUG = "bounded"
POSTING = "Build interfaces from Figma designs with React and TypeScript."


@pytest.fixture(name="server")
def server_fixture(postgres_template: str) -> Iterator[Server]:
    yield from copied_server(postgres_template)


@dataclass
class Account:
    """A hosted account holding track 1 only: its owner, and the run
    login's URL."""

    server: Server
    login: str
    owner: str


@pytest.fixture()
def account(server: Server) -> Iterator[Account]:
    with run_login_for(server.url) as login:
        yield Account(server, login, server.owner())


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A local store of the test's own, no `.env` from the checkout, and no
    account URL unless a test exports one."""
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    monkeypatch.delenv(ACCOUNT_URL_VARIABLE, raising=False)
    monkeypatch.setenv("HARRIER_DATABASE_URL", "")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@dataclass
class Result:
    code: int
    out: str
    err: str


def as_account(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    url: str,
    owner: str,
    *argv: str,
) -> Result:
    monkeypatch.setenv(ACCOUNT_URL_VARIABLE, url)
    code = main(["--owner", owner, *argv])
    out, err = capsys.readouterr()
    return Result(code, out, err)


def framing_text(framing: dict[str, Any]) -> str:
    return json.dumps(framing)


def focused_framing() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(FOCUSED_FRAMING.read_text(encoding="utf-8")))


def truth_lines() -> list[str]:
    """Every pool line of both example framings: a truth that supports each."""
    _, industry = example_documents()
    lines: list[str] = []
    for framing in (industry, focused_framing()):
        for text in cast("dict[str, str]", framing["bullet_pool"]).values():
            if text not in lines:
                lines.append(text)
    return lines


def seed_resume(conn: Connection) -> None:
    """The example facts, the industry framing on track 1 and a truth that
    supports every pool line."""
    store_documents(conn, *example_documents())
    put_document(conn, "resume_truth", "truth.md", "markdown", "\n".join(truth_lines()))


def seed_account(account: Account) -> None:
    conn = open_owner_store(account.server.url, account.owner)
    try:
        seed_resume(conn)
    finally:
        conn.close()


def hosted_tracks(account: Account, owner: str) -> list[dict[str, Any]]:
    return hosted_query(
        account.server,
        "SELECT id, slug, kind FROM tracks WHERE owner_id = %s ORDER BY id",
        (owner,),
    )


def hosted_documents(account: Account) -> list[dict[str, Any]]:
    return hosted_query(
        account.server,
        "SELECT kind, name, track_id, content FROM profile_documents WHERE owner_id = %s "
        "ORDER BY kind, name",
        (account.owner,),
    )


def write_file(directory: Path, name: str, text: str) -> str:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return str(path)


# --- the tests the spec names ---


def test_account_writes_adds_a_focused_track_to_one_account(
    account: Account, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    other = account.server.owner()

    added = as_account(
        monkeypatch, capsys, account.login, account.owner,
        "tracks", "add", SLUG, "--kind", "focused", "--label", "Bounded",
    )  # fmt: skip

    assert added.code == 0, added.err
    assert SLUG in added.out
    assert [(row["slug"], row["kind"]) for row in hosted_tracks(account, account.owner)] == [
        ("job", "industry"),
        (SLUG, "focused"),
    ]
    assert [row["slug"] for row in hosted_tracks(account, other)] == ["job"]

    # One active focused track per account, as locally (spec 137).
    second = as_account(
        monkeypatch, capsys, account.login, account.owner,
        "tracks", "add", "other", "--kind", "focused", "--label", "Other",
    )  # fmt: skip
    assert second.code == 2
    assert f"track {SLUG} is the focused track" in second.err
    assert len(hosted_tracks(account, account.owner)) == 2

    listed = as_account(monkeypatch, capsys, account.login, account.owner, "tracks", "list")
    assert listed.code == 0
    assert [line.split()[1] for line in listed.out.splitlines()] == ["job", SLUG]


def test_account_writes_puts_track_documents(
    account: Account,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed_account(account)
    run = partial(as_account, monkeypatch, capsys, account.login, account.owner)
    assert run("tracks", "add", SLUG, "--kind", "focused", "--label", "Bounded").code == 0
    focused_id = hosted_tracks(account, account.owner)[1]["id"]

    for kind, path in (
        ("screening", SCREENING),
        ("resume_framing", FOCUSED_FRAMING),
        ("application_profile", FOCUSED_PROFILE),
    ):
        put = run("--track", SLUG, "profile", "put", kind, "--file", str(path))
        assert put.code == 0, put.err

    owned = {row["kind"]: row for row in hosted_documents(account) if row["track_id"] == focused_id}
    assert set(owned) == {"screening", "resume_framing", "application_profile"}
    assert json.loads(owned["resume_framing"]["content"]) == focused_framing()

    checked = run("--track", SLUG, "profile", "check")
    assert checked.code == 0, checked.out + checked.err
    # The truth gate's line first; spec 164's advisory findings may follow it.
    lines = checked.out.splitlines()
    assert lines[0] == f"resume content for track {SLUG}: valid"
    assert lines[1:] == [] or lines[1] == "writing (advisory):"
    assert not [line for line in lines if "not supported by the truth documents" in line]

    # Four profile sentences on the focused kind: refused, nothing stored.
    framing = focused_framing()
    framing["profile_summary"] = "Ships interfaces. Reviews code. Writes tests."
    too_long = write_file(tmp_path, "long.json", framing_text(framing))
    refused = run("--track", SLUG, "profile", "put", "resume_framing", "--file", too_long)
    assert refused.code == 1
    assert "at most 3" in refused.err
    after = {row["kind"]: row for row in hosted_documents(account) if row["track_id"] == focused_id}
    assert after == owned

    listed = run("profile", "list")
    assert listed.code == 0
    assert "screening/screening.json (json, updated" in listed.out
    assert listed.out.splitlines()[-1] == f"{len(hosted_documents(account))} documents"


def test_account_writes_moves_a_job_on_both_stores(
    account: Account,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def add_job(conn: Connection) -> int:
        _, row = add_manually(
            conn,
            default_scope(conn),
            company="Example Studio",
            title="Junior Frontend Developer",
            location="Remote, Europe",
            url="https://jobs.example.com/example-studio",
            description=POSTING,
        )
        assert row is not None
        return int(row["id"])

    # SQLite, through the local store.
    assert main(["tracks", "add", SLUG, "--kind", "focused", "--label", "Bounded"]) == 0
    assert main(["--track", SLUG, "profile", "put", "screening", "--file", str(SCREENING)]) == 0
    local = connect()
    try:
        local_id = add_job(local)
    finally:
        local.close()
    capsys.readouterr()
    assert main(["move", str(local_id), "--to", SLUG]) == 0
    assert f"moved {local_id} from job to {SLUG}" in capsys.readouterr().out

    # Postgres, through the account.
    run = partial(as_account, monkeypatch, capsys, account.login, account.owner)
    assert run("tracks", "add", SLUG, "--kind", "focused", "--label", "Bounded").code == 0
    assert run("--track", SLUG, "profile", "put", "screening", "--file", str(SCREENING)).code == 0
    hosted = open_owner_store(account.server.url, account.owner)
    try:
        hosted_id = add_job(hosted)
    finally:
        hosted.close()

    moved = run("move", str(hosted_id), "--to", SLUG)

    assert moved.code == 0, moved.err
    assert moved.out.startswith(f"moved {hosted_id} from job to {SLUG}, score ")
    focused_id = hosted_tracks(account, account.owner)[1]["id"]
    [job] = hosted_query(account.server, "SELECT track_id FROM jobs WHERE id = %s", (hosted_id,))
    assert job["track_id"] == focused_id
    events = hosted_query(
        account.server,
        "SELECT kind, reason_code FROM job_events WHERE job_id = %s ORDER BY id",
        (hosted_id,),
    )
    assert (events[-1]["kind"], events[-1]["reason_code"]) == ("decision", MOVED)

    # A job not on the source track is refused, as locally (spec 138).
    again = run("move", str(hosted_id), "--to", SLUG)
    assert again.code == 1
    assert f"no job {hosted_id} on track job" in again.err


def test_profile_put_shared_documents_checks_every_track(
    account: Account,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    full = "\n".join(truth_lines())
    _, industry = example_documents()
    pool = cast("dict[str, str]", industry["bullet_pool"])
    dropped_id, dropped_text = next(iter(pool.items()))
    short = "\n".join(line for line in truth_lines() if line != dropped_text)
    full_path = write_file(tmp_path, "truth.md", full + "\nShipped a synthetic extra line.\n")
    short_path = write_file(tmp_path, "short.md", short)
    facts, _ = example_documents()
    facts_path = write_file(tmp_path, "facts.json", json.dumps(facts))
    not_json = write_file(tmp_path, "facts-bad.json", "[1, 2]")

    def stored_truth(conn: Connection) -> str:
        row = conn.execute(
            "SELECT content FROM profile_documents WHERE kind = 'resume_truth' AND track_id IS NULL"
        ).fetchone()
        assert row is not None
        return str(row[0])

    # SQLite, through the local store.
    local = connect()
    try:
        seed_resume(local)
    finally:
        local.close()
    capsys.readouterr()
    assert main(["profile", "put", "resume_truth", "--file", full_path]) == 0
    assert capsys.readouterr().out.splitlines() == ["stored resume_truth/truth.md"]
    assert main(["profile", "put", "resume_truth", "--file", short_path]) == 2
    err = capsys.readouterr().err
    assert "resume_truth not stored" in err
    assert f"track job: bullet {dropped_id}: not supported by the truth documents" in err
    local = connect()
    try:
        assert stored_truth(local) == full + "\nShipped a synthetic extra line.\n"
    finally:
        local.close()
    assert main(["profile", "put", "resume_facts", "--file", facts_path]) == 0
    assert main(["profile", "put", "resume_facts", "--file", not_json]) == 2
    assert main(["--track", "job", "profile", "put", "resume_facts", "--file", facts_path]) == 2
    assert "belongs to no track" in capsys.readouterr().err

    # Postgres, through the account: every track is checked, the focused one too.
    seed_account(account)
    run = partial(as_account, monkeypatch, capsys, account.login, account.owner)
    assert run("tracks", "add", SLUG, "--kind", "focused", "--label", "Bounded").code == 0
    put = run("--track", SLUG, "profile", "put", "resume_framing", "--file", str(FOCUSED_FRAMING))
    assert put.code == 0, put.err
    before = hosted_documents(account)

    refused = run("profile", "put", "resume_truth", "--file", short_path)

    assert refused.code == 2
    assert f"track job: bullet {dropped_id}" in refused.err
    assert hosted_documents(account) == before

    stored = run("profile", "put", "resume_truth", "--file", full_path)
    assert stored.code == 0, stored.err
    [truth] = [row for row in hosted_documents(account) if row["kind"] == "resume_truth"]
    assert truth["name"] == "truth.md"
    assert truth["content"].endswith("Shipped a synthetic extra line.\n")


def test_account_writes_refuses_before_writing(
    account: Account,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = hosted_tracks(account, account.owner)
    add = ("tracks", "add", SLUG, "--kind", "focused", "--label", "Bounded")

    # Unset: the variable is named, nothing opens.
    assert main(["--owner", account.owner, *add]) == 1
    assert f"{ACCOUNT_URL_VARIABLE} is not set" in capsys.readouterr().err

    # Set only in `.env`: never read from there.
    (tmp_path / ".env").write_text(f"{ACCOUNT_URL_VARIABLE}={account.login}\n", encoding="utf-8")
    assert main(["--owner", account.owner, *add]) == 1
    assert f"{ACCOUNT_URL_VARIABLE} is not set" in capsys.readouterr().err

    # The migrator's URL bypasses the row policy.
    migrator = as_account(monkeypatch, capsys, account.server.url, account.owner, *add)
    assert migrator.code == 1
    assert "login role" in migrator.err

    # A user id with no account sees no track.
    stranger = as_account(monkeypatch, capsys, account.login, str(uuid.uuid4()), *add)
    assert stranger.code == 1
    assert NO_ACCOUNT in stranger.err

    assert hosted_tracks(account, account.owner) == before

    # Not a user id: a usage error.
    with pytest.raises(SystemExit) as raised:
        main(["--owner", "someone@example.test", *add])
    assert raised.value.code == 2
    assert "example.test" not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "command"),
    [
        (["discover"], "discover"),
        (["tracks", "archive", SLUG], "tracks archive"),
        # `tailor` and `cover-letter` joined the commands in spec 147.
        (["answers", "--job-id", "1"], "answers"),
        (["store", "status"], "store status"),
    ],
)
def test_account_writes_refuses_other_commands(
    argv: list[str],
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A URL that names nothing reachable: a command that opened it would
    # report the connection, not the refusal.
    monkeypatch.setenv(ACCOUNT_URL_VARIABLE, "postgresql://u:p@127.0.0.1:1/db")
    assert main(["--owner", str(uuid.uuid4()), *argv]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err.strip() == f"error: {account_refusal(command)}"


def test_account_writes_prints_no_identity(
    account: Account, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stranger = str(uuid.uuid4())
    results = [
        as_account(
            monkeypatch, capsys, account.login, account.owner,
            "tracks", "add", SLUG, "--kind", "focused", "--label", "Bounded",
        ),
        as_account(monkeypatch, capsys, account.login, account.owner, "tracks", "list"),
        as_account(monkeypatch, capsys, account.login, account.owner, "profile", "list"),
        as_account(monkeypatch, capsys, account.login, stranger, "tracks", "list"),
        as_account(monkeypatch, capsys, account.server.url, account.owner, "tracks", "list"),
        as_account(
            monkeypatch, capsys, "postgresql://u:secret@unreachable.example.test:1/db",
            account.owner, "tracks", "list",
        ),
    ]  # fmt: skip
    assert [result.code for result in results] == [0, 0, 0, 1, 1, 1]
    host = account.login.split("@", 1)[1].split("/", 1)[0]
    password = account.login.split("@", 1)[0].rsplit(":", 1)[1]
    for result in results:
        text = result.out + result.err
        for name in (account.owner, stranger, host, password, "unreachable.example.test", "secret"):
            assert name not in text


def test_account_writes_ignores_the_local_container(
    account: Account, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("an account command asked about the local store")

    # Were the engine asked, a running container would own the local store.
    monkeypatch.setattr(container, "detect", refuse)
    monkeypatch.setattr(container, "running_in", lambda: "host")
    monkeypatch.setattr(cli, "remove_dead", refuse)
    monkeypatch.setattr(cli, "set_lease_subcommand", refuse)
    monkeypatch.setattr(cli, "_run_or_hand_over", refuse)
    monkeypatch.setattr(cli, "configure_logging", refuse)
    monkeypatch.setattr(cli, "connect", refuse)

    listed = as_account(monkeypatch, capsys, account.login, account.owner, "tracks", "list")

    assert listed.code == 0, listed.err
    assert listed.out.split()[1] == "job"


def test_profile_put_shared_documents_start_an_empty_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A store with no facts takes them first, then a framing, then the
    truth: a track whose content did not load cannot be broken. Once it
    loads, a facts edit that breaks its framing is refused, and an archived
    track's bullets are guarded like an active one's (spec 144, amendment
    2; the data-integrity review of PR #349)."""
    facts, industry = example_documents()
    focused = focused_framing()
    assert main(["tracks", "add", SLUG, "--kind", "focused", "--label", "Bounded"]) == 0

    def put(kind: str, path: str, *track: str) -> int:
        return main([*track, "profile", "put", kind, "--file", path])

    assert put("resume_facts", write_file(tmp_path, "facts.json", json.dumps(facts))) == 0
    assert put("resume_framing", write_file(tmp_path, "industry.json", json.dumps(industry))) == 0
    assert put("resume_truth", write_file(tmp_path, "truth.md", "\n".join(truth_lines()))) == 0
    focused_path = write_file(tmp_path, "focused.json", json.dumps(focused))
    assert put("resume_framing", focused_path, "--track", SLUG) == 0
    capsys.readouterr()

    # Facts that drop a role the framing names: the content no longer loads.
    roles = cast("list[dict[str, Any]]", facts["roles"])
    fewer = {**facts, "roles": roles[1:]}
    assert put("resume_facts", write_file(tmp_path, "fewer.json", json.dumps(fewer))) == 2
    err = capsys.readouterr().err
    assert "resume_facts not stored" in err
    assert "track job: " in err

    # An archived track's bullets still count.
    assert main(["tracks", "archive", SLUG]) == 0
    industry_lines = set(cast("dict[str, str]", industry["bullet_pool"]).values())
    focused_pool = cast("dict[str, str]", focused["bullet_pool"])
    only_focused = next(
        (bullet, text) for bullet, text in focused_pool.items() if text not in industry_lines
    )
    short = "\n".join(line for line in truth_lines() if line != only_focused[1])
    capsys.readouterr()
    assert put("resume_truth", write_file(tmp_path, "short.md", short)) == 2
    assert f"track {SLUG}: bullet {only_focused[0]}" in capsys.readouterr().err
