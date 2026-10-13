"""The domain's SQL means the same on both stores (spec 126, B1).

Each test takes the dual-store fixture (`store` or `dual_store`, in
`tests/conftest.py`) and runs once on SQLite and once on Postgres, there as
one synthetic owner under the real row policy. The Postgres parameter skips
locally without a test server and fails in CI without one.

The lock tests live with spec 079's, in `tests/test_job_events.py`. Every
row here is synthetic: invented companies, postings and people.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import DualStore
from pg_support import open_owner_store

from harrier import cutover
from harrier.apply.profile import load_candidate_document
from harrier.connection import (
    Connection,
    Cursor,
    StoreBusyError,
    StoreIntegrityError,
    StoreLocalOnlyError,
)
from harrier.profile import store as profile_store
from harrier.profile.store import get_document, list_documents, owned_documents, put_document
from harrier.resume import content as resume_content
from harrier.resume import split as resume_split
from harrier.runoutcome import record_success
from harrier.tracker import migrate_legacy
from harrier.tracker import store as tracker_store
from harrier.tracker.schema import HOSTED_ONLY_TABLES, OWNED_TABLES, TENANT_ROLE
from harrier.tracker.store import (
    DuplicateJobError,
    EligibilityAssessment,
    add_contact,
    add_job,
    find_duplicate,
    get_job,
    list_contacts,
    list_jobs,
    set_status,
    update_fields,
)
from harrier.tracks import DEFAULT_TRACK_ID, DuplicateTrackError, add_track, archive_track
from harrier.tracks import default_scope as scope_of
from harrier.userconfig.store import LINKEDIN_SEARCHES, list_config, set_config


def _job(conn: Connection, index: int = 1, **fields: str) -> int:
    return add_job(
        conn,
        {
            "company": f"Larchmere Analytics {index}",
            "title": "Data Engineer",
            "location": "Remote, Europe",
            "url": f"https://jobs.example.net/larchmere/{index}",
            "source": "greenhouse",
            **fields,
        },
        scope=scope_of(conn),
    )


def _record_statements(conn: Connection, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every statement the domain runs through `conn` from here on."""
    seen: list[str] = []
    real = conn.execute

    def execute(sql: str, params: Sequence[object] = ()) -> Cursor:
        seen.append(sql)
        return real(sql, params)

    monkeypatch.setattr(conn, "execute", execute)
    return seen


# --- timestamps ---------------------------------------------------------------

STORED_TIME = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def test_a_written_timestamp_has_one_shape_on_both_stores(store: Connection) -> None:
    """Every timestamp the domain writes is `store_now()`, bound as a
    parameter: SQLite's text, on Postgres too, which has no `datetime()`."""
    scope = scope_of(store)
    job_id = _job(store)
    written = {
        "set_status": set_status(store, scope, job_id, "shortlisted")["updated_at"],
        "update_fields": update_fields(store, scope, job_id, {"next_action": "read the brief"})[
            "updated_at"
        ],
    }
    add_track(store, "postdoc-search", "academic", "Postdoc search")
    written["archive_track"] = archive_track(store, "postdoc-search").archived_at
    set_config(store, LINKEDIN_SEARCHES, ["https://www.linkedin.com/jobs/search/?keywords=data"])
    written["set_config"] = list_config(store)[0]["updated_at"]
    put_document(store, "interview_prep", "notes.md", "markdown", "# Notes")
    written["put_document"] = list_documents(store)[0]["updated_at"]
    assert {name: bool(STORED_TIME.match(value)) for name, value in written.items()} == {
        name: True for name in written
    }, written


# --- case folding in the dedupe -------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "added", "duplicate"),
    [
        ("Acme", "ACME", True),
        # The É is the same character in both; only the ASCII letters fold.
        ("École Exemple", "ÉCOLE EXEMPLE", True),
        # É and é are different characters, and neither store folds them.
        ("École Exemple", "école exemple", False),
    ],
)
def test_dedupe_folds_ascii_case_only_on_both_stores(
    store: Connection, stored: str, added: str, duplicate: bool
) -> None:
    _job(store, 1, company=stored)
    for deadline in (None, ""):
        found = find_duplicate(store, "", added, "DATA ENGINEER", "", deadline=deadline)
        assert (found is not None) is duplicate, (added, deadline)
    if duplicate:
        with pytest.raises(DuplicateJobError):
            _job(store, 2, company=added)
    else:
        _job(store, 2, company=added)
        assert len(list_jobs(store, scope_of(store))) == 2


# --- text ordering --------------------------------------------------------------

# Written out of order. Code point order puts every capital before every
# lowercase letter; the database's collation on the test server and in CI
# (en_US.utf8) puts `b.md` before `B.md` and `a.md` before `Z.md`.
NAMES = ("b.md", "Z.md", "a.md", "B.md")
BY_CODE_POINT = ["B.md", "Z.md", "a.md", "b.md"]


def test_text_orders_by_code_point_on_both_stores(
    store: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in NAMES:
        put_document(store, "interview_prep", name, "markdown", f"# {name}")
        put_document(
            store, "application_profile", name, "markdown", f"# {name}", track_id=DEFAULT_TRACK_ID
        )
        stem = name.removesuffix(".md")
        put_document(store, "candidate", f"{stem}.json", "json", json.dumps({"name": stem}))
    documents = list_documents(store)
    assert [row["name"] for row in documents if row["kind"] == "interview_prep"] == BY_CODE_POINT
    owned = owned_documents(store, "application_profile", DEFAULT_TRACK_ID)
    assert [name for name, _ in owned] == BY_CODE_POINT
    names = resume_split._names(store, "interview_prep")  # pyright: ignore[reportPrivateUsage]
    assert names == BY_CODE_POINT
    # `LIMIT 1` by name picks the same document on both stores.
    assert load_candidate_document(store) == {"name": "B"}
    first = resume_content._document_by_kind(store, "interview_prep")  # pyright: ignore[reportPrivateUsage]
    assert first == "# B.md"
    # A shared document orders before an owned one of the same kind and
    # name: SQLite's default for a NULL, which Postgres takes from NULLS
    # FIRST. A shared application profile is one written before spec 101
    # made the kind a track's.
    with monkeypatch.context() as legacy:
        legacy.setattr(profile_store, "TRACK_OWNED_KINDS", frozenset[str]())
        put_document(store, "application_profile", "a.md", "markdown", "# shared")
    owners = [
        row["owner"]
        for row in list_documents(store)
        if (row["kind"], row["name"]) == ("application_profile", "a.md")
    ]
    assert owners == ["shared", f"track {scope_of(store).track.slug}"]
    set_config(store, LINKEDIN_SEARCHES, ["https://www.linkedin.com/jobs/search/?keywords=data"])
    set_config(store, "feeds", [])
    assert [row["kind"] for row in list_config(store)] == ["feeds", LINKEDIN_SEARCHES]


# --- new ids --------------------------------------------------------------------


def test_an_insert_returns_its_id_on_both_stores(store: Connection) -> None:
    first, second = _job(store, 1), _job(store, 2)
    assert first != second
    scope = scope_of(store)
    assert get_job(store, scope, first)["url"] == "https://jobs.example.net/larchmere/1"
    assert get_job(store, scope, second)["url"] == "https://jobs.example.net/larchmere/2"
    contact = add_contact(store, {"company": "Larchmere Analytics 1", "person_name": "Sam Example"})
    assert [(row["id"], row["person_name"]) for row in list_contacts(store)] == [
        (str(contact), "Sam Example")
    ]


_INSERT = re.compile(r"^\s*INSERT\s+INTO\s+(\w+)\s*\(([^)]*)\)", re.IGNORECASE)


def test_no_domain_insert_supplies_an_id(
    store: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every new id comes from the store. On Postgres the tenant role may not
    write `id` (spec 105, migration 13), so an insert naming it is refused
    there; on SQLite this test is what refuses it. Every owned table's
    writer runs, so a new owned table fails here until its insert does."""
    seen = _record_statements(store, monkeypatch)
    add_track(store, "postdoc-search", "academic", "Postdoc search")
    job_id = add_job(
        store,
        {"company": "Larchmere Analytics", "title": "Data Engineer", "url": "https://x.example"},
        scope=scope_of(store),
        eligibility=EligibilityAssessment(verdict="possible", checked_at="2026-10-11T09:30:00Z"),
    )
    set_status(store, scope_of(store), job_id, "shortlisted")
    add_contact(store, {"company": "Larchmere Analytics", "person_name": "Sam Example"})
    put_document(store, "interview_prep", "notes.md", "markdown", "# Notes")
    put_document(
        store, "application_profile", "profile.md", "markdown", "# P", track_id=DEFAULT_TRACK_ID
    )
    set_config(store, LINKEDIN_SEARCHES, ["https://www.linkedin.com/jobs/search/?keywords=data"])
    record_success(store, "discovery")
    columns: dict[str, set[str]] = {}
    for sql in seen:
        match = _INSERT.match(sql)
        if match is not None:
            names = {name.strip().lower() for name in match.group(2).split(",")}
            columns.setdefault(match.group(1).lower(), set()).update(names)
    # A hosted-only table has no writer that runs on SQLite: schedule_state
    # is written by spec 108's scheduler, on Postgres alone.
    assert set(columns) == set(OWNED_TABLES) - set(HOSTED_ONLY_TABLES)
    assert {table: "id" in names for table, names in columns.items()} == {
        table: False for table in columns
    }


def test_a_duplicate_is_refused_alike_on_both_stores(
    store: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_track(store, "postdoc-search", "academic", "Postdoc search")
    with pytest.raises(DuplicateTrackError):
        add_track(store, "postdoc-search", "academic", "Postdoc search again")
    job_id = _job(store, 1)

    # A writer that passed the dedupe before this one inserted: the unique
    # index is the authority.
    def no_duplicate(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(tracker_store, "find_duplicate", no_duplicate)
    with pytest.raises(DuplicateJobError, match="unique index"):
        _job(store, 1)
    assert [row["id"] for row in list_jobs(store, scope_of(store))] == [str(job_id)]


# --- upserts ------------------------------------------------------------------


@dataclass(frozen=True)
class Upsert:
    name: str
    table: str
    write: Callable[[Connection, str], None]
    read: Callable[[Connection], list[str]]
    values: tuple[str, str]


def _column(sql: str, params: tuple[object, ...]) -> Callable[[Connection], list[str]]:
    return lambda conn: [str(row[0]) for row in conn.execute(sql, params)]


UPSERTS = [
    Upsert(
        "record_success",
        "job_runs",
        lambda conn, value: record_success(conn, "discovery", at=value),
        _column("SELECT last_success_at FROM job_runs WHERE job = ?", ("discovery",)),
        ("2026-10-01T09:00:00Z", "2026-10-02T09:00:00Z"),
    ),
    Upsert(
        "set_config",
        "user_config",
        lambda conn, value: set_config(conn, LINKEDIN_SEARCHES, [value]),
        lambda conn: [
            json.loads(str(row[0]))[0]
            for row in conn.execute(
                "SELECT value FROM user_config WHERE kind = ?", (LINKEDIN_SEARCHES,)
            )
        ],
        (
            "https://www.linkedin.com/jobs/search/?keywords=first",
            "https://www.linkedin.com/jobs/search/?keywords=second",
        ),
    ),
    Upsert(
        "put_document shared",
        "profile_documents",
        lambda conn, value: put_document(conn, "interview_prep", "notes.md", "markdown", value),
        _column(
            "SELECT content FROM profile_documents WHERE kind = ? AND name = ?",
            ("interview_prep", "notes.md"),
        ),
        ("# first", "# second"),
    ),
    Upsert(
        "put_document owned",
        "profile_documents",
        lambda conn, value: put_document(
            conn, "application_profile", "profile.md", "markdown", value, track_id=DEFAULT_TRACK_ID
        ),
        _column(
            "SELECT content FROM profile_documents WHERE kind = ? AND name = ?",
            ("application_profile", "profile.md"),
        ),
        ("# first", "# second"),
    ),
]


@dataclass(frozen=True)
class _Step:
    """One statement of an upsert on its table: an update and the rows it
    changed, or an insert and whether the store refused it."""

    who: str
    kind: str
    changed: int | None = None
    refused: bool = False


def _trace(
    conn: Connection,
    who: str,
    table: str,
    steps: list[_Step],
    monkeypatch: pytest.MonkeyPatch,
    before: Callable[[str], None] = lambda kind: None,
) -> None:
    """Record each update and insert `conn` runs on `table`, calling
    `before` with its kind first."""
    real = conn.execute

    def execute(sql: str, params: Sequence[object] = ()) -> Cursor:
        head = sql.lstrip()
        if head.startswith(f"UPDATE {table} "):
            before("update")
            cursor = real(sql, params)
            steps.append(_Step(who, "update", changed=cursor.rowcount))
            return cursor
        if head.startswith(f"INSERT INTO {table} "):
            before("insert")
            try:
                cursor = real(sql, params)
            except StoreIntegrityError:
                steps.append(_Step(who, "insert", refused=True))
                raise
            steps.append(_Step(who, "insert"))
            return cursor
        return real(sql, params)

    monkeypatch.setattr(conn, "execute", execute)


@pytest.mark.parametrize("upsert", UPSERTS, ids=[upsert.name for upsert in UPSERTS])
def test_an_upsert_racing_another_leaves_one_row(
    dual_store: DualStore, monkeypatch: pytest.MonkeyPatch, upsert: Upsert
) -> None:
    """Two writers of one key at once: the rival starts after the writer's
    update found no row and before the writer's insert. One row on both
    stores, holding the later writer's value, and each store's path is
    asserted step by step.

    Postgres: the writer holds no lock on a key it has not inserted, so the
    rival updates nothing, inserts and commits. The writer's insert is then
    refused by the unique key, rolls back to its savepoint, and the retried
    update changes the rival's row. The writer wrote last. This is the
    store where the retry and the savepoint are reached.

    SQLite cannot reach the retry: the writer's update began BEGIN
    IMMEDIATE, so the rival is shown to meet the held write lock, waits,
    and its update then changes the writer's committed row, with no insert.
    The rival wrote last.
    """
    writer = dual_store.open()
    mine, theirs = upsert.values
    steps: list[_Step] = []
    updating = threading.Event()
    lock_held: list[bool] = []
    failures: list[BaseException] = []

    def rival_writes() -> None:
        # Opened on this thread, as SQLite requires, and closed here.
        rival = dual_store.open(keep=False)
        try:
            if dual_store.dialect == "sqlite":
                # The writer holds the write lock now: a connection that will
                # not wait cannot take it.
                probe = dual_store.open(keep=False)
                probe.busy_timeout_ms = 0
                try:
                    with probe.transaction(immediate=True):
                        lock_held.append(False)
                except StoreBusyError:
                    lock_held.append(True)
                finally:
                    probe.close()

            def first_update(kind: str) -> None:
                if kind == "update":
                    updating.set()

            _trace(rival, "rival", upsert.table, steps, monkeypatch, first_update)
            upsert.write(rival, theirs)
        except BaseException as error:
            failures.append(error)
        finally:
            updating.set()
            rival.close()

    thread = threading.Thread(target=rival_writes)

    def race_before_the_insert(kind: str) -> None:
        if kind != "insert" or thread.ident is not None:
            return
        thread.start()
        updating.wait(timeout=10)
        if dual_store.dialect == "postgres":
            thread.join(timeout=10)
        else:
            # The rival's update is issued and waits on the lock this
            # connection holds until its commit.
            time.sleep(0.2)

    _trace(writer, "writer", upsert.table, steps, monkeypatch, race_before_the_insert)
    upsert.write(writer, mine)
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert failures == []
    if dual_store.dialect == "postgres":
        assert steps == [
            _Step("writer", "update", changed=0),
            _Step("rival", "update", changed=0),
            _Step("rival", "insert"),
            _Step("writer", "insert", refused=True),
            _Step("writer", "update", changed=1),
        ]
        later = mine
    else:
        assert lock_held == [True]
        assert steps == [
            _Step("writer", "update", changed=0),
            _Step("writer", "insert"),
            _Step("rival", "update", changed=1),
        ]
        later = theirs
    assert upsert.read(dual_store.open()) == [later]


def test_a_shared_and_an_owned_document_upsert_alike_on_both_stores(
    store: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shared and an owned document of one kind and name are two rows,
    each written and rewritten through its own update. The shared update
    matches `track_id IS NULL` in its text; one that bound a None to
    `track_id = ?` would match nothing, and the rewrite would fail on the
    shared partial index. A shared row of an owned kind is one written
    before spec 101 made the kind a track's."""
    kind, name = "application_profile", "profile.md"

    def put_shared(content: str) -> None:
        with monkeypatch.context() as legacy:
            legacy.setattr(profile_store, "TRACK_OWNED_KINDS", frozenset[str]())
            put_document(store, kind, name, "markdown", content)

    def put_owned(content: str) -> None:
        put_document(store, kind, name, "markdown", content, track_id=DEFAULT_TRACK_ID)

    def both() -> tuple[str | None, list[tuple[str, str]]]:
        return get_document(store, kind, name), owned_documents(store, kind, DEFAULT_TRACK_ID)

    put_owned("# owned, first")
    put_shared("# shared, first")
    assert both() == ("# shared, first", [(name, "# owned, first")])
    put_owned("# owned, second")
    assert both() == ("# shared, first", [(name, "# owned, second")])
    put_shared("# shared, second")
    assert both() == ("# shared, second", [(name, "# owned, second")])
    owners = [
        row["owner"] for row in list_documents(store) if (row["kind"], row["name"]) == (kind, name)
    ]
    assert owners == ["shared", f"track {scope_of(store).track.slug}"]


# --- local-only work -----------------------------------------------------------

CUTOVER_REFUSAL = (
    "harrier cutover is local only: it moves the old system onto the local tracker file"
)
MIGRATE_REFUSAL = (
    "harrier migrate-legacy is local only: it imports the old system's CSVs into the local "
    "tracker file"
)


def test_local_only_work_refuses_a_postgres_connection(
    store: Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cutover and the legacy import move the old system onto the local
    tracker file. Given a Postgres connection they refuse before any
    statement runs, rather than write those rows into an owner's store."""
    never_loaded: cutover.LaunchctlFn = lambda args: (1, "", "")  # noqa: E731
    calls: dict[str, Callable[[], object]] = {
        "preflight": lambda: cutover.preflight(store, old_root=tmp_path, launchctl=never_loaded),
        "run_cutover": lambda: cutover.run_cutover(
            store, old_root=tmp_path, stamp="2026-10-12-0900", launchctl=never_loaded
        ),
        "tracker_check": lambda: cutover.tracker_check(store),
        "verify": lambda: cutover.verify(store, result=cutover.CutoverResult(executed=False)),
    }
    if store.dialect == "sqlite":
        # The local store is the one these are for.
        assert cutover.tracker_check(store).passed is False
        return
    seen = _record_statements(store, monkeypatch)
    for name, call in calls.items():
        with pytest.raises(StoreLocalOnlyError) as refused:
            call()
        assert str(refused.value) == CUTOVER_REFUSAL, name
    with pytest.raises(StoreLocalOnlyError) as refused:
        migrate_legacy.migrate(store, tmp_path / "jobs.csv", None)
    assert str(refused.value) == MIGRATE_REFUSAL
    assert seen == []


# --- the fixture ----------------------------------------------------------------


def test_the_store_fixture_runs_as_the_tenant(dual_store: DualStore) -> None:
    """The Postgres connection acts as the owner under the row policy. A lost
    role switch would run every test as the migrating role, which bypasses
    the policy, so the fixture fails a connection that is not the tenant."""
    conn = dual_store.open()
    if dual_store.dialect == "sqlite":
        assert conn.dialect == "sqlite"
        return
    with conn.transaction():
        assert tuple(conn.execute("SELECT current_user").fetchone()) == (TENANT_ROLE,)
    # Opened with no owner, the connection runs as the migrating role.
    with pytest.raises(pytest.fail.Exception, match=f"not {TENANT_ROLE}"):
        open_owner_store(dual_store.url, None)


MARKED_TESTS = """
import pytest


@pytest.mark.sqlite_only("drives the CLI")
def test_marked(dual_store):
    assert dual_store.dialect == "sqlite"


@pytest.mark.sqlite_only("reads SQLite itself")
def test_marked_without_the_fixture():
    pass


def test_unmarked(dual_store):
    pass
"""

UNEXPLAINED_TEST = """
import pytest


@pytest.mark.sqlite_only("")
def test_unexplained(dual_store):
    pass
"""


def test_a_sqlite_only_test_gets_the_sqlite_parameter_alone(pytester: pytest.Pytester) -> None:
    """The mark B2 puts on a test that stays on SQLite (spec 126, Amendment
    (2026-10-12): what B2 settled). A marked test that takes the fixture is
    collected once, on SQLite; an unmarked one twice; and a mark with no
    reason stops the collection."""
    pytester.makeconftest(Path(__file__).with_name("conftest.py").read_text(encoding="utf-8"))
    pytester.makepyfile(test_marked=MARKED_TESTS)

    collected = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--collect-only", "-q")

    assert collected.ret == pytest.ExitCode.OK
    ids = {line.strip() for line in collected.outlines if "::" in line}
    assert ids == {
        "test_marked.py::test_marked[sqlite]",
        "test_marked.py::test_marked_without_the_fixture",
        "test_marked.py::test_unmarked[sqlite]",
        "test_marked.py::test_unmarked[postgres]",
    }

    pytester.makepyfile(test_marked=UNEXPLAINED_TEST)
    refused = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--collect-only", "-q")

    # Raised while the module is collected, so pytest reports a collection
    # error and runs nothing.
    assert refused.ret == pytest.ExitCode.INTERRUPTED
    refused.stdout.fnmatch_lines(["*test_unexplained is marked sqlite_only with no reason*"])
