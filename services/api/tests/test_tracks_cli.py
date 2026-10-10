"""An academic track can be added and its positions tracked by hand (spec 093).

Every database is built under `tmp_path`, and every track, position, company
and deadline is synthetic (spec 060, ADR-008).
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from academic_support import ACADEMIC_FIXTURE, academic_search_entry
from conftest import auth
from fastapi.testclient import TestClient

import harrier.capture as capture_module
import harrier_cli.main as cli_module
from harrier.db import connect
from harrier.tracker import TrackerError, add_job, list_jobs, set_status, update_fields
from harrier.tracker.queue import rank_by_deadline
from harrier.tracker.schema import NEXT_ACTION_DEFAULTS, STATUSES
from harrier.tracker.store import list_events
from harrier.tracks import KIND_RULES, Scope, default_scope, list_tracks, resolve_scope
from harrier.userconfig.store import ACADEMIC_SEARCHES, set_config
from harrier_api.app import create_app
from harrier_cli.main import build_parser, main, subcommand_name

SLUG = "second-search"


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "data"
    monkeypatch.setenv("HARRIER_DATA_DIR", str(directory))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    return directory


@pytest.fixture()
def academic(data_dir: Path, capsys: pytest.CaptureFixture[str]) -> Iterator[Path]:
    assert main(["tracks", "add", SLUG, "--kind", "academic", "--label", "Second search"]) == 0
    capsys.readouterr()
    yield data_dir


def scopes(conn: sqlite3.Connection) -> tuple[Scope, Scope]:
    return default_scope(conn), resolve_scope(conn, SLUG)


def add_position(
    company: str, *, deadline: str | None = None, url: str = "", slug: str = SLUG
) -> int:
    argv = ["--track", slug, "add", "--company", company, "--title", "Research Engineer"]
    if url:
        argv += ["--url", url]
    if deadline:
        argv += ["--deadline", deadline]
    return main(argv)


def only_row(conn: sqlite3.Connection, scope: Scope) -> dict[str, str]:
    found = list_jobs(conn, scope)
    assert len(found) == 1, found
    return found[0]


# --- adding and the industry score -------------------------------------------


def test_add_lands_in_the_named_track(academic: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        add_position("Example Lab", deadline="2027-11-30", url="https://calls.example.com/1") == 0
    )
    assert "Added: Example Lab" in capsys.readouterr().out
    assert main(["tracks", "list"]) == 0
    listed = capsys.readouterr().out.splitlines()
    assert any(SLUG in line and "academic" in line for line in listed)
    conn = connect()
    try:
        first, second = scopes(conn)
        row = only_row(conn, second)
        assert row["company"] == "Example Lab"
        assert row["deadline"] == "2027-11-30"
        assert row["status"] == "prospect"
        assert list_jobs(conn, first) == []
    finally:
        conn.close()


def test_an_academic_add_stores_no_industry_score(
    academic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: object, **__: object) -> Any:
        raise AssertionError("the academic add reached the industry scoring path")

    with monkeypatch.context() as patched:
        for name in (
            "load_candidate_config",
            "fit_score_for",
            "enrich_job_description_for_scoring",
        ):
            patched.setattr(capture_module, name, refuse)
        assert add_position("Example Lab", url="https://calls.example.com/1") == 0

    conn = connect()
    try:
        second = scopes(conn)[1]
        row = only_row(conn, second)
        for column in ("fit_score", "score", "signals", "scoring_version", "remote_filter"):
            assert row[column] == "", column
        created = list_events(conn, second, int(row["id"]))[0]
        assert created["kind"] == "created"
        assert created["fit_score"] == "" and created["scoring_version"] == ""
    finally:
        conn.close()

    # The same add on the industry track is scored, as it always was.
    assert (
        main(
            [
                "add",
                "--company",
                "Example Co",
                "--title",
                "Senior Frontend Engineer",
                "--url",
                "https://boards.example.com/x/1",
                "--description",
                "Fully remote across Europe. TypeScript and React.",
            ]
        )
        == 0
    )
    conn = connect()
    try:
        industry = only_row(conn, default_scope(conn))
        assert industry["fit_score"] != "" and industry["scoring_version"] != ""
    finally:
        conn.close()


# --- statuses on an academic track --------------------------------------------


def test_marking_an_academic_row_applied_seeds_no_follow_up(academic: Path) -> None:
    assert add_position("Example Lab") == 0
    conn = connect()
    try:
        job_id = only_row(conn, scopes(conn)[1])["id"]
    finally:
        conn.close()
    assert main(["--track", SLUG, "applied", job_id, "--applied-date", "2027-01-15"]) == 0
    conn = connect()
    try:
        row = only_row(conn, scopes(conn)[1])
    finally:
        conn.close()
    assert row["status"] == "applied"
    assert row["applied_date"] == "2027-01-15"
    assert row["next_action"] == KIND_RULES["academic"].next_action["applied"]
    assert "follow up" not in row["next_action"]
    for column in (
        "last_contact",
        "outreach_status",
        "next_outreach_action",
        "contacts_found",
        "outreach_priority",
    ):
        assert row[column] == "", column


def test_status_labels_follow_the_track_kind(
    academic: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for kind, rules in KIND_RULES.items():
        assert set(rules.labels) == set(STATUSES), kind
        assert set(rules.next_action) == set(STATUSES), kind
    # The industry kind is today's behaviour, from one definition. Its labels
    # are the browser's words, which spec 094 moved here; only the one status
    # whose stored name is not a word reads differently.
    assert dict(KIND_RULES["industry"].next_action) == NEXT_ACTION_DEFAULTS
    assert {
        status: label for status, label in KIND_RULES["industry"].labels.items() if label != status
    } == {"tailored_cv_requested": "CV requested"}

    assert add_position("Example Lab") == 0
    capsys.readouterr()
    assert main(["--track", SLUG, "next"]) == 0
    assert "[found]" in capsys.readouterr().out
    conn = connect()
    try:
        job_id = only_row(conn, scopes(conn)[1])["id"]
    finally:
        conn.close()
    assert main(["--track", SLUG, "track", job_id]) == 0
    assert "[preparing documents]" in capsys.readouterr().out
    conn = connect()
    try:
        # The stored status is the shared lifecycle; only the words differ.
        assert only_row(conn, scopes(conn)[1])["status"] == "tailored_cv_requested"
    finally:
        conn.close()


# --- the deadline queue --------------------------------------------------------


def test_academic_queue_orders_by_nearest_open_deadline(
    academic: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [
        {"id": "1", "status": "prospect", "deadline": "2027-03-10"},
        {"id": "2", "status": "prospect", "deadline": ""},
        {"id": "3", "status": "shortlisted", "deadline": "2027-02-01"},
        {"id": "4", "status": "prospect", "deadline": "2027-03-05"},
        {"id": "5", "status": "rejected", "deadline": "2027-03-02"},
    ]
    ranked = rank_by_deadline(rows, today="2027-03-01")
    # Nearest open deadline first, then no deadline, then the passed one,
    # which is sunk and still shown. Rejected rows are never queued.
    assert [row["id"] for row in ranked] == ["4", "1", "2", "3"]

    # Through the command: a passed deadline is flagged and listed last.
    assert add_position("Long Gone Lab", deadline="2020-01-02") == 0
    assert add_position("Far Ahead Lab", deadline="2099-01-01") == 0
    capsys.readouterr()
    assert main(["--track", SLUG, "next"]) == 0
    out = capsys.readouterr().out
    assert out.index("Far Ahead Lab") < out.index("Long Gone Lab")
    assert "deadline: 2020-01-02  (deadline passed)" in out
    assert "deadline: 2099-01-01\n" in out


# --- the allowlist ---------------------------------------------------------------

# `discover` and `reconsider` left this list with spec 097: an academic
# track discovers from its own search and reconsiders its own seen state.
REFUSED: list[list[str]] = [
    ["reevaluate", "1"],
    ["evaluate-prospects"],
    ["scoring", "train"],
    ["scoring", "export"],
    ["find-contacts", "--job-id", "1"],
    ["tailor", "--job-id", "1"],
    ["cover-letter", "--job-id", "1"],
    ["answers", "--job-id", "1"],
    ["evaluate", "--job-id", "1"],
    ["outreach-draft", "--job-id", "1"],
    ["company-outcome", "1", "ghosted"],
    ["events", "backfill"],
    ["digest"],
    ["gmail-watch"],
    ["check"],
]


def _tracing(monkeypatch: pytest.MonkeyPatch, statements: list[str]) -> None:
    real_connect = sqlite3.connect

    def tracing_connect(database: Any, **kwargs: Any) -> sqlite3.Connection:
        conn = real_connect(database, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracing_connect)


def test_commands_outside_the_allowlist_refuse_a_non_default_track(
    academic: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert add_position("Example Lab") == 0
    capsys.readouterr()
    statements: list[str] = []
    with monkeypatch.context() as patched:
        _tracing(patched, statements)
        for argv in REFUSED:
            statements.clear()
            assert main(["--track", SLUG, *argv]) == 2, argv
            err = capsys.readouterr().err
            name = subcommand_name(build_parser().parse_args(argv))
            assert f"harrier {name}: not available on track {SLUG}" in err, (argv, err)
            assert SLUG in err, (argv, err)
            # Refused before the command read a single row.
            assert not any("FROM jobs" in statement for statement in statements), argv

    # Every command inside the allowlist runs on the same track.
    conn = connect()
    try:
        job_id = only_row(conn, scopes(conn)[1])["id"]
    finally:
        conn.close()
    allowed = [
        ["add", "--company", "Another Lab", "--title", "Engineer"],
        ["tracks", "list"],
        ["next"],
        ["review"],
        ["shortlist", job_id],
        ["track", job_id],
        ["applied", job_id],
        ["interviewing", job_id],
        ["reject", job_id, "not", "a", "fit"],
        ["events", "show", job_id],
        ["export", "--dest", str(academic.parent / "export")],
    ]
    for argv in allowed:
        assert main(["--track", SLUG, *argv]) == 0, argv
        assert "not available on track" not in capsys.readouterr().err, argv


# --- the track verbs and the flag ---------------------------------------------


def test_tracks_add_refuses_a_second_industry_track(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["tracks", "add", "more-jobs", "--kind", "industry", "--label", "More"]) == 2
    assert "second industry track" in capsys.readouterr().err
    conn = connect()
    try:
        assert [track.slug for track in list_tracks(conn)] == ["job"]
    finally:
        conn.close()


def test_track_lifecycle_refusals(academic: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # A slug already in use.
    assert main(["tracks", "add", SLUG, "--kind", "academic", "--label", "Again"]) == 1
    # The default track cannot be archived.
    assert main(["tracks", "archive", "job"]) == 2
    # An unknown slug, named with the command that lists them.
    assert main(["--track", "no-such-track", "next"]) == 2
    assert "tracks list" in capsys.readouterr().err

    assert add_position("Example Lab") == 0
    assert main(["tracks", "archive", SLUG]) == 0
    assert main(["tracks", "archive", SLUG]) == 1
    capsys.readouterr()
    # Archived: still lists and reads, refuses writes, writes nothing.
    assert main(["--track", SLUG, "next"]) == 0
    assert "Example Lab" in capsys.readouterr().out
    assert add_position("Later Lab") == 2
    assert "archived" in capsys.readouterr().err
    conn = connect()
    try:
        job_id = only_row(conn, scopes(conn)[1])["id"]
    finally:
        conn.close()
    assert main(["--track", SLUG, "shortlist", job_id]) == 2
    conn = connect()
    try:
        row = only_row(conn, scopes(conn)[1])
        assert row["company"] == "Example Lab" and row["status"] == "prospect"
    finally:
        conn.close()


def test_the_default_tracks_slug_is_the_same_as_no_flag(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["add", "--company", "Example Co", "--title", "Engineer"]) == 0
    capsys.readouterr()
    assert main(["next"]) == 0
    without = capsys.readouterr().out
    assert main(["--track", "job", "next"]) == 0
    assert capsys.readouterr().out == without
    # The default track's commands are not narrowed by naming it.
    assert main(["--track", "job", "digest", "--dry-run"]) == 0
    # A malformed slug is a usage error before anything runs.
    with pytest.raises(SystemExit) as usage:
        main(["--track", "Not A Slug", "next"])
    assert usage.value.code == 2


# --- the deadline ------------------------------------------------------------------


def test_a_malformed_deadline_is_refused(academic: Path) -> None:
    for bad in ("2027-02-30", "27-01-01", "2027/01/01", "soon"):
        with pytest.raises(SystemExit) as usage:
            add_position("Example Lab", deadline=bad)
        assert usage.value.code == 2, bad
    conn = connect()
    try:
        second = scopes(conn)[1]
        assert list_jobs(conn, second) == []
        # The write path refuses a date that does not exist; the database
        # refuses a shape that is not a date at all.
        with pytest.raises(TrackerError, match="deadline"):
            add_job(conn, {"company": "A", "title": "B", "deadline": "2027-02-30"}, scope=second)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO jobs (company, title, deadline) VALUES ('A', 'B', '2027/01/01')"
            )
    finally:
        conn.close()


# --- what the academic track never reads ----------------------------------------


def test_academic_commands_read_no_profile_document(
    academic: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No allowed command reads a profile document on an academic track, and
    the only configuration row any of them reads is `discover`'s own search
    (spec 097 narrows spec 093 to that one row). Logging setup's redaction
    read is the one shared read by design, named in the spec, and is left
    out of what is traced here. `profile put` and `profile check` read the
    track's resume content by design (spec 099) and are not run here; their
    reads are pinned in test_track_framing.py."""
    assert add_position("Example Lab") == 0
    conn = connect()
    try:
        job_id = only_row(conn, scopes(conn)[1])["id"]
        set_config(conn, ACADEMIC_SEARCHES, {SLUG: academic_search_entry()})
    finally:
        conn.close()
    capsys.readouterr()

    def no_logging(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(cli_module, "configure_logging", no_logging)
    statements: list[str] = []
    _tracing(monkeypatch, statements)
    for argv in (
        ["add", "--company", "Another Lab", "--title", "Engineer", "--deadline", "2027-05-01"],
        ["next"],
        ["review"],
        ["shortlist", job_id],
        ["track", job_id],
        ["applied", job_id],
        ["interviewing", job_id],
        ["reject", job_id, "closed"],
        ["events", "show", job_id],
        ["export", "--dest", str(academic.parent / "export")],
        ["tracks", "list"],
        ["discover", "--dataset-file", str(ACADEMIC_FIXTURE), "--no-notify"],
    ):
        assert main(["--track", SLUG, *argv]) == 0, argv
    read = [s for s in statements if "profile_documents" in s or "user_config" in s]
    # The one configuration read: the academic search's own row.
    assert read, "discover read no configuration at all"
    assert all(
        "user_config" in s and "profile_documents" not in s and ACADEMIC_SEARCHES in s for s in read
    ), read


# --- export and the browser ---------------------------------------------------------


def test_export_on_a_non_default_track_writes_under_its_slug(
    academic: Path, tmp_path: Path
) -> None:
    assert add_position("Example Lab") == 0
    dest = tmp_path / "export"
    assert main(["--track", SLUG, "export", "--dest", str(dest)]) == 0
    track_file = dest / SLUG / "jobs.csv"
    assert track_file.is_file()
    assert "Example Lab" in track_file.read_text(encoding="utf-8")
    assert not list(dest.rglob("contacts.csv"))

    default_dest = tmp_path / "export-default"
    assert main(["export", "--dest", str(default_dest)]) == 0
    assert (default_dest / "jobs.csv").is_file() and (default_dest / "contacts.csv").is_file()
    assert "Example Lab" not in (default_dest / "jobs.csv").read_text(encoding="utf-8")


def test_browser_capture_lands_in_the_default_track(academic: Path) -> None:
    client: TestClient = TestClient(create_app())
    response = client.post(
        "/capture/add",
        json={"company": "Browser Co", "title": "Platform Engineer"},
        headers=auth(),
    )
    assert response.status_code == 200
    response = client.post(
        "/tracker",
        json={"company": "Form Co", "title": "Product Engineer"},
        headers=auth(),
    )
    assert response.status_code in (200, 201)
    conn = connect()
    try:
        first, second = scopes(conn)
        assert {row["company"] for row in list_jobs(conn, first)} == {"Browser Co", "Form Co"}
        assert list_jobs(conn, second) == []
    finally:
        conn.close()


# --- review of PR #182 ------------------------------------------------------


def test_the_store_refuses_writes_on_an_archived_track(academic: Path) -> None:
    """The CLI refuses first with exit 2; the store holds for every other
    caller, so an archived track cannot be written by a path that skips the
    command line."""
    assert add_position("Example Lab") == 0
    assert main(["tracks", "archive", SLUG]) == 0
    conn = connect()
    try:
        archived = resolve_scope(conn, SLUG)
        assert archived.track.archived
        row = only_row(conn, archived)
        with pytest.raises(TrackerError, match="archived"):
            add_job(conn, {"company": "Later Lab", "title": "Engineer"}, scope=archived)
        with pytest.raises(TrackerError, match="archived"):
            set_status(conn, archived, int(row["id"]), "shortlisted")
        with pytest.raises(TrackerError, match="archived"):
            update_fields(conn, archived, int(row["id"]), {"notes": "touched"})
        after = only_row(conn, archived)
        assert after["status"] == "prospect" and after["notes"] == row["notes"]
    finally:
        conn.close()


def test_a_repeated_archive_is_its_own_refusal(academic: Path) -> None:
    """Exit status 1 for a repeated archive comes from the exception's type,
    not from the words in its message."""
    from harrier.tracks import AlreadyArchivedError, TrackRefusedError, archive_track

    conn = connect()
    try:
        archive_track(conn, SLUG)
        with pytest.raises(AlreadyArchivedError):
            archive_track(conn, SLUG)
        with pytest.raises(TrackRefusedError) as refused:
            archive_track(conn, "job")
        assert not isinstance(refused.value, AlreadyArchivedError)
    finally:
        conn.close()
