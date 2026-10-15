"""A local store imports into one empty hosted account (spec 141).

Each test builds a synthetic local store under its own tmp_path and a copy of
the migrated Postgres template (`postgres_template`, spec 126), makes a run
login for it, and drives `harrier store import-local` through `main`. Every
company, person and document is invented; ids are chosen so that a local id
and its hosted id differ, since a copy that forgot to rewrite one would
otherwise pass. The tests that need Postgres skip locally without a test
server and fail in CI (spec 039).
"""

# Pyright strict cannot resolve psycopg's cursor description and row types.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pg_support import open_owner_store
from test_hosted_requests import Server, copied_server, run_login_for

import harrier.tracker.importlocal as importlocal
from harrier import container, db
from harrier.container import ContainerState
from harrier.db import connect, default_db_path
from harrier.pgstore import postgres_connect
from harrier.tracker.importlocal import IMPORT_URL_VARIABLE, TABLES
from harrier_cli.main import main

UNREACHABLE = ContainerState(engine=container.UNREACHABLE)
ORIGINAL_LABEL = "Job search"

# Names that stand for identity values: none may reach the output.
SENTINELS = (
    "Sentinel Person One",
    "sentinel.one@example.test",
    "Example Co One",
    "Synthetic Client",
)


@pytest.fixture(name="server")
def server_fixture(postgres_template: str) -> Iterator[Server]:
    yield from copied_server(postgres_template)


@dataclass
class Account:
    """An empty hosted account: its owner, and the run login's URL."""

    server: Server
    login: str
    owner: str


@pytest.fixture()
def account(server: Server) -> Iterator[Account]:
    with run_login_for(server.url) as login:
        yield Account(server, login, server.owner())


@dataclass
class Result:
    code: int
    out: str
    err: str


def run_import(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    url: str,
    owner: str,
) -> Result:
    monkeypatch.setenv(IMPORT_URL_VARIABLE, url)
    code = main(["store", "import-local", "--owner", owner])
    out, err = capsys.readouterr()
    return Result(code, out, err)


# --- the synthetic local store ---


def put(conn: Any, table: str, **values: object) -> None:
    names = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    conn.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})", list(values.values()))


def build_local_store(path: Path) -> None:
    """Three tracks (ids 1, 4 and 9), jobs on tracks 1 and 9, events and
    verdicts, contacts, documents for a track and for none, configuration and
    run records. Local ids are far from the ids a fresh hosted account hands
    out, and event ids do not follow event times."""
    conn = connect(path)
    with conn.transaction():
        conn.execute(
            "UPDATE tracks SET label = ?, created_at = ? WHERE id = 1",
            ("Search A", "2026-01-01 00:00:00"),
        )
        put(
            conn,
            "tracks",
            id=4,
            slug="bounded",
            kind="focused",
            label="Bounded search",
            created_at="2026-02-03 04:05:06",
            archived_at="2026-04-05 06:07:08",
        )
        put(
            conn,
            "tracks",
            id=9,
            slug="papers",
            kind="academic",
            label="Papers",
            created_at="2026-03-04 05:06:07",
            archived_at=None,
        )
        for job in (
            (101, "Example Co One", "Frontend Developer", "shortlisted", 1, ""),
            (102, "Example Co Two", "Backend Developer", "applied", 1, ""),
            (205, "Example Lab", "Research Fellow", "prospect", 9, "2026-06-30"),
        ):
            number, company, title, status, track, deadline = job
            put(
                conn,
                "jobs",
                id=number,
                company=company,
                title=title,
                location="Remote, Europe",
                url=f"https://example.test/jobs/{number}",
                source="fixture",
                added_at="2026-01-10",
                fit_score="72",
                applied_date="2026-01-20" if status == "applied" else "",
                notes=f"synthetic note {number}",
                status=status,
                external_key=f"ext-{number}",
                created_at="2026-01-10 08:00:00",
                updated_at="2026-01-11 09:00:00",
                track_id=track,
                deadline=deadline,
            )
        events = (
            (11, 205, "2026-03-01T10:00:00Z", "created", "system", "", "prospect"),
            (12, 101, "2026-01-10T08:00:00Z", "created", "system", "", "prospect"),
            (13, 101, "2026-01-11T09:00:00Z", "decision", "candidate", "prospect", "shortlisted"),
            (14, 102, "2026-01-12T07:30:00Z", "created", "system", "", "prospect"),
            (15, 102, "2026-01-20T10:30:00Z", "decision", "candidate", "prospect", "applied"),
            (16, 102, "2026-02-02T11:45:00Z", "outcome", "company", "applied", "rejected"),
        )
        for number, job, at, kind, actor, before, after in events:
            put(
                conn,
                "job_events",
                id=number,
                job_id=job,
                at=at,
                kind=kind,
                actor=actor,
                from_status=before,
                to_status=after,
                reason_text=f"synthetic reason {number}",
                fit_score="72",
                scoring_version="v-test",
                description_sha256="ab" * 32,
            )
        verdicts = (
            (31, 102, "confirmed", "", "candidate", "2026-02-01T09:00:00Z"),
            (32, 101, "possible", "contractor|eor", "rule", "2026-01-12T09:00:00Z"),
        )
        for number, job, verdict, routes, origin, checked in verdicts:
            put(
                conn,
                "job_eligibility",
                id=number,
                job_id=job,
                verdict=verdict,
                routes=routes,
                evidence_url="https://example.test/evidence",
                excerpt="synthetic excerpt",
                field="description" if routes else "",
                matched="eu",
                checked_at=checked,
                origin=origin,
                version="v1",
                description_sha256="cd" * 32,
                at=checked,
            )
        links = [
            {
                "job_id": "205",
                "company": "Example Lab",
                "job_title": "Research Fellow",
                "job_url": "https://example.test/jobs/205",
            },
            {"job_id": "999", "company": "Gone Co", "job_title": "Gone", "job_url": ""},
            {"job_id": "", "company": "Never Tracked", "job_title": "Role", "job_url": ""},
        ]
        put(
            conn,
            "contacts",
            id=51,
            company="Example Co One",
            person_name="Sentinel Person One",
            person_email="sentinel.one@example.test",
            linked_jobs=json.dumps(links),
            created_at="2026-01-15 10:00:00",
            updated_at="2026-01-16 11:00:00",
        )
        put(conn, "contacts", id=52, company="Example Co Two", person_name="Second Person")
        put(
            conn,
            "profile_documents",
            id=61,
            kind="resume_data",
            name="resume.json",
            format="json",
            content='{"synthetic": true}',
            updated_at="2026-01-02 03:04:05",
            track_id=None,
        )
        put(
            conn,
            "profile_documents",
            id=62,
            kind="application_profile",
            name="profile.json",
            format="json",
            content='{"track": "bounded"}',
            track_id=4,
        )
        put(
            conn,
            "profile_documents",
            id=63,
            kind="application_brief",
            name="101",
            format="json",
            content='{"never_name": ["Synthetic Client"]}',
            track_id=None,
        )
        put(conn, "user_config", id=71, kind="discovery", value='{"sources": []}')
        put(conn, "user_config", id=72, kind="company_holds", value='["Example Co One"]')
        put(conn, "job_runs", job="discover", last_success_at="2026-03-05T06:00:00Z")
        put(conn, "job_runs", job="digest", last_success_at="2026-03-06T06:00:00Z")
    conn.close()
    db.release_host_lease()


@pytest.fixture()
def local(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The local store at the data directory's `tracker.db`, treated as the
    live one: spec 061's check and spec 075's lease apply to it."""
    path = default_db_path()
    build_local_store(path)
    monkeypatch.setattr(db, "live_data_root", lambda: path.parent)
    monkeypatch.setattr(container, "detect", lambda timeout=0.0: UNREACHABLE)
    monkeypatch.setattr(container, "running_in", lambda: "host")
    yield path
    db.release_host_lease()


def local_rows(path: Path, table: str) -> list[dict[str, Any]]:
    order = "job" if table == "job_runs" else "id"
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as raw:
        cursor = raw.execute(f"SELECT * FROM {table} ORDER BY {order}")
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


# --- the hosted account, read as the migrator, which bypasses the policy ---


def hosted_query(server: Server, sql: str, params: tuple[object, ...] = ()) -> list[dict[str, Any]]:
    with closing(postgres_connect(server.url)) as conn:
        cursor = conn.execute(sql.encode(), params)
        assert cursor.description is not None
        names = [column.name for column in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def hosted_rows(server: Server, table: str, owner: str) -> list[dict[str, Any]]:
    order = "job" if table == "job_runs" else "id"
    return hosted_query(
        server, f"SELECT * FROM {table} WHERE owner_id = %s ORDER BY {order}", (owner,)
    )


def snapshot(server: Server, owner: str) -> dict[str, list[dict[str, Any]]]:
    return {table: hosted_rows(server, table, owner) for table in TABLES}


def assert_untouched(account: Account, before: dict[str, list[dict[str, Any]]]) -> None:
    after = snapshot(account.server, account.owner)
    assert after == before
    assert [row["label"] for row in after["tracks"]] == [ORIGINAL_LABEL]


def same_values(
    local: list[dict[str, Any]], hosted: list[dict[str, Any]], skip: tuple[str, ...]
) -> None:
    assert len(local) == len(hosted)
    for expected, found in zip(local, hosted, strict=True):
        for column, value in expected.items():
            if column not in skip:
                assert found[column] == value, column


def insert_as_owner(account: Account, owner: str, sql: str, params: Sequence[object] = ()) -> None:
    conn = open_owner_store(account.server.url, owner)
    try:
        conn.execute(sql, params)
    finally:
        conn.close()


# --- the nine tests the spec names ---


def test_import_local_copies_every_table_and_rewrites_ids(
    account: Account,
    local: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = run_import(monkeypatch, capsys, account.login, account.owner)
    assert result.code == 0, result.err
    owned = {table: hosted_rows(account.server, table, account.owner) for table in TABLES}

    # Tracks: local 1, 4 and 9 become hosted 1, 2 and 3, in local id order.
    tracks = owned["tracks"]
    assert [row["id"] for row in tracks] == [1, 2, 3]
    same_values(local_rows(local, "tracks"), tracks, skip=("id",))
    track_map = {1: 1, 4: 2, 9: 3}

    # Jobs: new ids, the track rewritten, every other column as written.
    local_jobs = local_rows(local, "jobs")
    jobs = owned["jobs"]
    same_values(local_jobs, jobs, skip=("id", "track_id"))
    job_map = {old["id"]: new["id"] for old, new in zip(local_jobs, jobs, strict=True)}
    assert all(old != new for old, new in job_map.items())
    for old, new in zip(local_jobs, jobs, strict=True):
        assert new["track_id"] == track_map[old["track_id"]]

    # Events and verdicts point at the hosted job that carries the same key.
    for table in ("job_events", "job_eligibility"):
        old_rows = local_rows(local, table)
        new_rows = owned[table]
        same_values(old_rows, new_rows, skip=("id", "job_id"))
        assert [row["job_id"] for row in new_rows] == [job_map[row["job_id"]] for row in old_rows]

    # Documents: a track's keeps pointing at its track, a shared one stays null.
    old_documents = local_rows(local, "profile_documents")
    documents = owned["profile_documents"]
    same_values(old_documents, documents, skip=("id", "track_id", "name"))
    assert [row["track_id"] for row in documents] == [None, track_map[4], None]

    for table in ("contacts", "user_config", "job_runs"):
        skip = ("id", "linked_jobs") if table == "contacts" else ("id",)
        same_values(local_rows(local, table), owned[table], skip=skip)

    assert all(str(row["owner_id"]) == account.owner for rows in owned.values() for row in rows), (
        "every imported row belongs to the named owner"
    )


def test_import_local_keeps_history_times_and_order(
    account: Account,
    local: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_import(monkeypatch, capsys, account.login, account.owner).code == 0
    keys = {row["id"]: row["external_key"] for row in local_rows(local, "jobs")}
    hosted_keys = {
        row["id"]: row["external_key"] for row in hosted_rows(account.server, "jobs", account.owner)
    }
    for table, extra in (("job_events", "kind"), ("job_eligibility", "verdict")):
        expected = [
            (keys[row["job_id"]], row["at"], row[extra]) for row in local_rows(local, table)
        ]
        found = [
            (hosted_keys[row["job_id"]], row["at"], row[extra])
            for row in hosted_rows(account.server, table, account.owner)
        ]
        assert found == expected
    # The ids are not time order here, so a copy that sorted by time or
    # reversed the rows would not reproduce the sequence.
    ats = [row["at"] for row in local_rows(local, "job_events")]
    assert ats != sorted(ats)


def test_import_local_writes_only_the_named_owner(
    account: Account,
    local: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    other = account.server.owner()
    insert_as_owner(account, other, "INSERT INTO jobs (company, url) VALUES (?, ?)", ["B Co", "u"])
    insert_as_owner(account, other, "INSERT INTO contacts (company) VALUES (?)", ["B Co"])
    insert_as_owner(
        account, other, "INSERT INTO user_config (kind, value) VALUES (?, ?)", ["k", "v"]
    )
    others_before = snapshot(account.server, other)

    assert run_import(monkeypatch, capsys, account.login, account.owner).code == 0

    assert snapshot(account.server, other) == others_before
    for table in TABLES:
        strangers = hosted_query(
            account.server,
            f"SELECT count(*) AS n FROM {table} WHERE owner_id NOT IN (%s, %s)",
            (account.owner, other),
        )
        assert strangers[0]["n"] == 0, table
        mine = hosted_rows(account.server, table, account.owner)
        assert len(mine) == len(local_rows(local, table)), table
    mine_ids = {row["id"] for row in hosted_rows(account.server, "jobs", account.owner)}
    theirs_ids = {row["id"] for row in others_before["jobs"]}
    assert mine_ids.isdisjoint(theirs_ids)


def test_import_local_refuses_an_account_with_rows(
    server: Server,
    local: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cases = (
        ("tracks", "INSERT INTO tracks (slug, kind, label) VALUES ('extra', 'industry', 'X')"),
        ("jobs", "INSERT INTO jobs (company) VALUES ('Held Co')"),
        ("contacts", "INSERT INTO contacts (company) VALUES ('Held Co')"),
        ("profile_documents", "INSERT INTO profile_documents (kind, name) VALUES ('k', 'n')"),
        ("user_config", "INSERT INTO user_config (kind, value) VALUES ('k', 'v')"),
        ("job_runs", "INSERT INTO job_runs (job, last_success_at) VALUES ('j', 't')"),
    )
    with run_login_for(server.url) as login:
        for table, insert in cases:
            account = Account(server, login, server.owner())
            insert_as_owner(account, account.owner, insert)
            before = snapshot(server, account.owner)
            result = run_import(monkeypatch, capsys, login, account.owner)
            assert result.code == 1, table
            assert f"already holds data: {table} holds" in result.err, result.err
            assert result.out == ""
            after = snapshot(server, account.owner)
            assert after == before, table
            assert after["tracks"][0]["label"] == ORIGINAL_LABEL
        # A used account is named by the first table that holds a row, and an
        # event cannot exist without its job.
        account = Account(server, login, server.owner())
        insert_as_owner(account, account.owner, "INSERT INTO jobs (company) VALUES ('Held Co')")
        insert_as_owner(
            account,
            account.owner,
            "INSERT INTO job_events (job_id, kind, actor, to_status) "
            "SELECT id, 'created', 'system', 'prospect' FROM jobs",
        )
        result = run_import(monkeypatch, capsys, login, account.owner)
        assert result.code == 1
        assert "already holds data: jobs holds" in result.err


def test_import_local_rolls_back_whole_and_runs_again(
    account: Account,
    local: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = snapshot(account.server, account.owner)
    # SQLite keeps a NUL in text; Postgres refuses it, on the last table
    # written, after every other table has been copied.
    with closing(sqlite3.connect(local)) as raw, raw:
        raw.execute(
            "INSERT INTO job_runs (job, last_success_at) VALUES (?, ?)", ("bad\x00job", "t")
        )
    first = run_import(monkeypatch, capsys, account.login, account.owner)
    assert first.code == 1
    assert first.out == ""
    assert len(first.err.strip().splitlines()) == 1
    assert_untouched(account, before)

    with closing(sqlite3.connect(local)) as raw, raw:
        raw.execute("DELETE FROM job_runs WHERE job LIKE 'bad%'")
    second = run_import(monkeypatch, capsys, account.login, account.owner)
    assert second.code == 0, second.err
    for table in TABLES:
        assert len(hosted_rows(account.server, table, account.owner)) == len(
            local_rows(local, table)
        ), table


def test_import_local_rolls_back_when_the_account_changes(
    account: Account,
    local: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = importlocal.refuse_if_the_account_changed

    def after_another_writer(conn: Any, written: Any) -> None:
        # The web app, as the same owner on another connection, commits a
        # row after the import's last insert and before its commit.
        insert_as_owner(account, account.owner, "INSERT INTO contacts (company) VALUES ('Late Co')")
        original(conn, written)

    monkeypatch.setattr(importlocal, "refuse_if_the_account_changed", after_another_writer)
    result = run_import(monkeypatch, capsys, account.login, account.owner)
    assert result.code == 1
    assert "changed during the import" in result.err
    after = snapshot(account.server, account.owner)
    assert [row["company"] for row in after["contacts"]] == ["Late Co"]
    for table in TABLES:
        if table not in ("tracks", "contacts"):
            assert after[table] == [], table
    assert [row["label"] for row in after["tracks"]] == [ORIGINAL_LABEL]


def test_import_local_refuses_before_writing(
    account: Account,
    local: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = snapshot(account.server, account.owner)

    # A running container owns the local file (spec 061).
    monkeypatch.setattr(
        container,
        "detect",
        lambda timeout=0.0: ContainerState(
            engine=container.REACHABLE, running=True, data_source=local.parent
        ),
    )
    refused = run_import(monkeypatch, capsys, account.login, account.owner)
    assert refused.code == 1
    assert "container owns the local store" in refused.err
    assert refused.out == ""
    assert_untouched(account, before)
    monkeypatch.setattr(container, "detect", lambda timeout=0.0: UNREACHABLE)

    # The migrator's URL bypasses the row policy: a login the import refuses.
    refused = run_import(monkeypatch, capsys, account.server.url, account.owner)
    assert refused.code == 1
    assert "login role" in refused.err
    assert refused.out == ""
    assert_untouched(account, before)

    # An owner that does not exist sees no track.
    refused = run_import(monkeypatch, capsys, account.login, str(uuid.uuid4()))
    assert refused.code == 1
    assert "sees no track" in refused.err
    assert refused.out == ""
    assert_untouched(account, before)

    # A store one version behind: the sentence says what to do.
    with closing(sqlite3.connect(local)) as raw, raw:
        raw.execute(
            "DELETE FROM schema_version WHERE version = (SELECT MAX(version) FROM schema_version)"
        )
    refused = run_import(monkeypatch, capsys, account.login, account.owner)
    assert refused.code == 1
    assert "start the local app once" in refused.err
    assert refused.out == ""
    assert_untouched(account, before)


def test_import_local_never_writes_the_local_store(
    account: Account,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = default_db_path()
    build_local_store(path)
    # A store in the rollback journal's mode: opening it through
    # `harrier.db.connect()` would switch it to WAL and change its header.
    with closing(sqlite3.connect(path)) as raw:
        raw.execute("PRAGMA journal_mode=DELETE")
    monkeypatch.setattr(db, "live_data_root", lambda: path.parent)
    monkeypatch.setattr(container, "detect", lambda timeout=0.0: UNREACHABLE)
    monkeypatch.setattr(container, "running_in", lambda: "host")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    modified = path.stat().st_mtime_ns

    result = run_import(monkeypatch, capsys, account.login, account.owner)

    assert result.code == 0, result.err
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert path.stat().st_mtime_ns == modified
    db.release_host_lease()


def test_import_local_prints_no_identity(
    account: Account,
    local: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stranger = str(uuid.uuid4())
    refused = run_import(monkeypatch, capsys, account.login, stranger)
    assert refused.code == 1
    done = run_import(monkeypatch, capsys, account.login, account.owner)
    assert done.code == 0
    counts = [f"{table}: {len(local_rows(local, table))} copied" for table in TABLES]
    assert done.out.splitlines() == [*counts, "imported"]
    assert done.err == ""
    host = account.login.split("@", 1)[1].split("/", 1)[0]
    password = account.login.split("@", 1)[0].rsplit(":", 1)[1]
    for text in (done.out + done.err, refused.out + refused.err):
        for name in (account.owner, stranger, host, password, *SENTINELS):
            assert name not in text


# --- the amendment and the checks the nine do not name ---


def test_import_local_rewrites_job_ids_named_inside_rows(
    account: Account,
    local: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_import(monkeypatch, capsys, account.login, account.owner).code == 0
    jobs = {
        row["external_key"]: row["id"] for row in hosted_rows(account.server, "jobs", account.owner)
    }
    contact = hosted_rows(account.server, "contacts", account.owner)[0]
    links = json.loads(contact["linked_jobs"])
    # A link to a job the store holds follows the job. One to a job it does
    # not hold, and an unresolved one, name no job, and keep their text.
    assert links[0]["job_id"] == str(jobs["ext-205"])
    assert links[0]["company"] == "Example Lab"
    assert links[1] == {"job_id": "", "company": "Gone Co", "job_title": "Gone", "job_url": ""}
    assert links[2]["job_id"] == ""
    # The brief is named by the job it belongs to.
    briefs = [
        row
        for row in hosted_rows(account.server, "profile_documents", account.owner)
        if row["kind"] == "application_brief"
    ]
    assert [row["name"] for row in briefs] == [str(jobs["ext-101"])]
    assert briefs[0]["content"] == '{"never_name": ["Synthetic Client"]}'


def test_import_local_refuses_a_brief_for_no_job(
    account: Account,
    local: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with closing(sqlite3.connect(local)) as raw, raw:
        raw.execute("UPDATE profile_documents SET name = '777' WHERE kind = 'application_brief'")
    before = snapshot(account.server, account.owner)
    result = run_import(monkeypatch, capsys, account.login, account.owner)
    assert result.code == 1
    assert "application brief" in result.err
    assert_untouched(account, before)


def test_import_local_refuses_a_column_the_hosted_store_lacks(
    account: Account,
    local: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = snapshot(account.server, account.owner)
    with closing(sqlite3.connect(local)) as raw, raw:
        raw.execute("ALTER TABLE jobs ADD COLUMN extra_note TEXT NOT NULL DEFAULT ''")
    refused = run_import(monkeypatch, capsys, account.login, account.owner)
    assert refused.code == 1
    assert "the hosted jobs table has no column extra_note" in refused.err
    assert_untouched(account, before)

    with closing(sqlite3.connect(local)) as raw, raw:
        raw.execute("ALTER TABLE jobs DROP COLUMN extra_note")
    account.server.migrator("ALTER TABLE contacts ADD COLUMN extra_note text NOT NULL")
    refused = run_import(monkeypatch, capsys, account.login, account.owner)
    assert refused.code == 1
    assert "the hosted contacts table has a column extra_note with no default" in refused.err
    assert_untouched(account, before)


def test_import_local_refuses_without_a_url_or_a_local_store(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    owner = str(uuid.uuid4())
    monkeypatch.delenv(IMPORT_URL_VARIABLE, raising=False)
    assert main(["store", "import-local", "--owner", owner]) == 1
    assert IMPORT_URL_VARIABLE in capsys.readouterr().err

    # `HARRIER_DATABASE_URL` is not the import's URL, whatever it names.
    monkeypatch.setenv("HARRIER_DATABASE_URL", "postgresql://u:p@127.0.0.1:1/db")
    assert main(["store", "import-local", "--owner", owner]) == 1
    assert IMPORT_URL_VARIABLE in capsys.readouterr().err
    monkeypatch.setenv("HARRIER_DATABASE_URL", "")

    monkeypatch.setenv(IMPORT_URL_VARIABLE, "postgresql://u:p@127.0.0.1:1/db")
    assert main(["store", "import-local", "--owner", owner]) == 1
    assert "local store" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [[], ["--owner", "a-person@example.test"], ["--owner", ""]])
def test_import_local_usage_errors_exit_2(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["store", "import-local", *argv])
    assert raised.value.code == 2
    assert "example.test" not in capsys.readouterr().err
