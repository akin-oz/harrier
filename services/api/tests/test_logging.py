"""Log configuration and identity redaction (spec 045).

The privacy plan said the logging setup installed a redaction filter for
candidate and contact identity values. Nothing did: there was no
`logging.Filter` in the tree at all. These tests exist so the sentence in
docs/privacy-plan.md has something behind it.

Each test exercises the decision rather than the helper. A test that called
`IdentityRedactionFilter` directly would keep passing if `configure_logging`
stopped installing it, which is the exact shape of failure this repository
keeps hitting.
"""

from __future__ import annotations

import gc
import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from harrier.connection import Connection, StoreDatabaseError
from harrier.db import connect
from harrier.logredact import REDACTION, IdentityRedactionFilter, identity_values
from harrier.logsetup import configure_logging

CANDIDATE = {
    "name": "Deniz Örnek",
    "email": "deniz@example.com",
    "phone": "+00 000 000 0000",
    "linkedin": "https://linkedin.com/in/deniz-ornek",
}


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Connection:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    conn = connect()
    import json

    with conn.transaction():
        conn.execute(
            "INSERT INTO profile_documents (kind, name, format, content) VALUES (?, ?, ?, ?)",
            ("resume_data", "bundle", "json", json.dumps({"candidate": CANDIDATE})),
        )
        conn.execute(
            "INSERT INTO contacts (person_name, person_email) VALUES (?, ?)",
            ("Jordan Sample", "jordan@example.com"),
        )
    return conn


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Configure logging for real, then read what a handler actually emits."""
    written: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            written.append(self.format(record))

    configure_logging(force=True)
    root = logging.getLogger()
    capture = Capture()
    capture.setFormatter(logging.Formatter("%(message)s"))
    # The filters configure_logging installed live on its handlers, so borrow
    # them: this proves the filter was installed, not merely that it works.
    for handler in root.handlers:
        for existing in handler.filters:
            capture.addFilter(existing)
    root.addHandler(capture)
    monkeypatch.setattr(root, "handlers", [capture])
    return written


def test_identity_values_reads_candidate_and_contacts(db: Connection) -> None:
    values = identity_values(db)
    assert CANDIDATE["name"] in values
    assert CANDIDATE["email"] in values
    assert "Jordan Sample" in values
    assert "jordan@example.com" in values


def test_redaction_reads_identity_from_either_shape(db: Connection) -> None:
    """Before the split the values are in `resume_data`, after it in
    `resume_facts`; both are read, so no point in between leaves one
    unredacted (spec 098)."""
    import json

    # The copy the split keeps can hold a value the facts have since replaced.
    with db.transaction():
        for kind, name in (("resume_facts", "Ada Ex"), ("resume_data_presplit", "Old Ex")):
            db.execute(
                "INSERT INTO profile_documents (kind, name, format, content) VALUES (?, ?, ?, ?)",
                (kind, f"{kind}.json", "json", json.dumps({"candidate": {"name": name}})),
            )
    assert {"Ada Ex", "Old Ex", CANDIDATE["name"]} <= identity_values(db)


def test_identity_values_survives_a_database_without_the_tables(tmp_path: Path) -> None:
    """A fresh clone has no profile store. Redaction must degrade, not raise."""
    bare = Connection(sqlite3.connect(tmp_path / "bare.db"))
    assert identity_values(bare) == set()


def test_a_short_identity_is_redacted_on_a_word_boundary() -> None:
    """An earlier version skipped anything under four characters, so a
    two-letter name reached the log untouched. The compiled privacy rule
    grants no such exemption (review of PR #49)."""
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "note from Jo today", None, None)
    IdentityRedactionFilter({"Jo"}).filter(record)
    assert "Jo" not in record.getMessage()
    assert REDACTION in record.getMessage()


def test_a_short_value_inside_another_word_is_left_alone() -> None:
    """The reason the length floor existed: redacting `Jo` by substring turns
    every `Join`, `Jobs` and `Jordan` into noise. A boundary match keeps the
    redaction and drops the shredding."""
    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "Jobs joined the Johnson queue", None, None
    )
    IdentityRedactionFilter({"Jo"}).filter(record)
    assert record.getMessage() == "Jobs joined the Johnson queue"


def test_a_single_character_value_is_still_ignored(tmp_path: Path) -> None:
    """One character is not identifying and matches as its own word constantly:
    an initial, a column left as `a`."""
    conn = Connection(sqlite3.connect(tmp_path / "short.db"))
    with conn.transaction():
        conn.execute(
            "CREATE TABLE contacts (person_name TEXT, person_email TEXT, linkedin_url TEXT)"
        )
        conn.execute("INSERT INTO contacts VALUES ('J', '', '')")
    assert identity_values(conn) == set()


def test_a_contact_added_after_startup_is_redacted(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The API runs for days, so a contact added after configure_logging is
    the normal case. It used to reach the log unredacted until restart, which
    the spec accepted as a limitation and the review rightly refused."""
    from harrier.tracker.store import add_contact

    written = _capture(monkeypatch)
    add_contact(db, {"person_name": "Wilhelmina Latecomer", "company": "Exampleco"})

    logging.getLogger("harrier.outreach").info("drafted for Wilhelmina Latecomer")

    assert written, "nothing was logged"
    assert "Wilhelmina Latecomer" not in written[0]
    assert REDACTION in written[0]


def test_the_candidate_name_does_not_reach_a_log_line(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = _capture(monkeypatch)
    logging.getLogger("harrier.somewhere").warning("draft prepared for %s", CANDIDATE["name"])
    assert written, "nothing was logged"
    assert CANDIDATE["name"] not in written[0]
    assert REDACTION in written[0]


def test_a_contact_address_does_not_reach_a_log_line(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = _capture(monkeypatch)
    logging.getLogger("harrier.outreach").info("queued jordan@example.com")
    assert written, "nothing was logged"
    assert "jordan@example.com" not in written[0]


def test_an_unrelated_line_is_left_alone(db: Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    written = _capture(monkeypatch)
    logging.getLogger("harrier.discovery").info("fetched 15 postings from exampleco")
    assert written[0] == "fetched 15 postings from exampleco"


# --- what a refresh keeps, and which writes refresh (spec 045, amendment of 2026-10-13) ---


def _logged(written: list[str], text: str) -> str:
    """Log `text` through the configured handlers and return what came out."""
    before = len(written)
    logging.getLogger("harrier.outreach").info(text)
    assert len(written) == before + 1, "nothing was logged"
    return written[-1]


def _contact_id(db: Connection, name: str) -> int:
    row = db.execute("SELECT id FROM contacts WHERE person_name = ?", (name,)).fetchone()
    assert row is not None
    return int(row[0])


def test_a_refresh_whose_read_fails_keeps_the_values_it_held(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refresh whose candidate read failed used to replace the set with the
    contacts alone, so the candidate's name reached the log in clear."""
    from harrier.tracker.store import add_contact

    written = _capture(monkeypatch)
    db.execute("ALTER TABLE profile_documents RENAME TO profile_documents_gone")
    add_contact(db, {"person_name": "Wilhelmina Latecomer", "company": "Exampleco"})

    assert CANDIDATE["name"] not in _logged(written, f"drafted for {CANDIDATE['name']}")
    assert "Wilhelmina Latecomer" not in _logged(written, "drafted for Wilhelmina Latecomer")


def test_a_refresh_keeps_a_value_the_store_no_longer_holds(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deleted contact is missing from the next read; the refresh after a
    later add used to drop its name."""
    from harrier.tracker.store import add_contact, delete_contact

    written = _capture(monkeypatch)
    assert delete_contact(db, _contact_id(db, "Jordan Sample"))
    add_contact(db, {"person_name": "Wilhelmina Latecomer", "company": "Exampleco"})

    assert "Jordan Sample" not in _logged(written, "replied to Jordan Sample")


def test_an_empty_refresh_drops_nothing() -> None:
    redaction = IdentityRedactionFilter({"Wilhelmina Latecomer"})
    redaction.refresh(set())
    record = logging.LogRecord("x", logging.INFO, "", 0, "for Wilhelmina Latecomer", (), None)
    redaction.filter(record)
    assert record.getMessage() == f"for {REDACTION}"


def test_an_edited_contact_is_redacted_at_once(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harrier.tracker.store import update_contact_fields

    written = _capture(monkeypatch)
    update_contact_fields(db, _contact_id(db, "Jordan Sample"), {"person_name": "Jordan Renamed"})

    assert "Jordan Renamed" not in _logged(written, "replied to Jordan Renamed")


def test_a_changed_candidate_name_is_redacted_at_once(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from harrier.profile.store import put_document

    written = _capture(monkeypatch)
    facts = json.dumps({"candidate": {"name": "Deniz Yenisoy"}})
    put_document(db, "resume_facts", "facts", "json", facts)

    assert "Deniz Yenisoy" not in _logged(written, "draft prepared for Deniz Yenisoy")


def test_a_split_profile_name_is_redacted_at_once(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from harrier.profile.store import put_documents_and_rekind

    written = _capture(monkeypatch)
    facts = json.dumps({"candidate": {"name": "Deniz Yenisoy"}})
    put_documents_and_rekind(
        db,
        [("resume_facts", "facts", "json", facts, None)],
        ("resume_data", "bundle", "resume_data_presplit"),
    )

    assert "Deniz Yenisoy" not in _logged(written, "draft prepared for Deniz Yenisoy")


def test_an_imported_contact_is_redacted_at_once(
    db: Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import csv

    from harrier.tracker.migrate_legacy import migrate
    from harrier.tracker.schema import TRACKER_FIELDS

    jobs_csv = tmp_path / "jobs.csv"
    with jobs_csv.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerow(TRACKER_FIELDS)
    contacts_csv = tmp_path / "contacts.csv"
    with contacts_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["person_name", "company"])
        writer.writerow(["Imported Sample", "Exampleco"])

    written = _capture(monkeypatch)
    migrate(db, jobs_csv, contacts_csv)

    assert "Imported Sample" not in _logged(written, "replied to Imported Sample")


def test_a_failed_identity_write_does_not_refresh(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A value the store holds but the filter has not read shows whether a
    refresh ran: the failed write must leave it unread."""
    import json

    from harrier.profile.store import ProfileDocumentError, put_documents_and_rekind

    written = _capture(monkeypatch)
    with db.transaction():
        db.execute(
            "INSERT INTO contacts (person_name, person_email) VALUES (?, ?)",
            ("Unread Sample", "unread@example.com"),
        )
    facts = json.dumps({"candidate": {"name": "Deniz Yenisoy"}})
    with pytest.raises(ProfileDocumentError):
        put_documents_and_rekind(
            db,
            [("resume_facts", "facts", "json", facts, None)],
            ("resume_data", "no_such_bundle", "resume_data_presplit"),
        )

    assert "Unread Sample" in _logged(written, "replied to Unread Sample")


def test_the_longest_value_is_redacted_first() -> None:
    """The address contains the local part; redacting the shorter one first
    would leave a fragment of the longer behind."""
    values = {"deniz@example.com", "deniz"}
    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "mail to deniz@example.com", None, None
    )
    IdentityRedactionFilter(values).filter(record)
    assert "example.com" not in record.getMessage()


def test_the_api_configures_logging_when_the_app_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """logsetup's docstring said the API called it. Only the CLI did, so the
    process serving the browser had no configured root and no redaction."""
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    calls: list[bool] = []

    def record(*_args: object, **_kwargs: object) -> None:
        calls.append(True)

    monkeypatch.setattr("harrier_api.app.configure_logging", record)
    from harrier_api.app import create_app

    create_app()
    assert calls, "create_app did not configure logging"


def test_an_exception_traceback_does_not_carry_an_identity(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`record.getMessage()` excludes the exception, so logger.exception()
    carried identity values straight past the filter. Demonstrated on the
    review of PR #50 with the value intact in `exc_text`."""
    written = _capture(monkeypatch)
    try:
        raise ValueError(f"could not draft for {CANDIDATE['name']}")
    except ValueError:
        logging.getLogger("harrier.apply").exception("draft failed")

    assert written, "nothing was logged"
    combined = written[0]
    assert CANDIDATE["name"] not in combined
    assert REDACTION in combined


def test_a_cached_exception_text_is_redacted() -> None:
    """The second handler reuses `exc_text` the first formatter cached, so an
    unredacted cache leaks to every sink after the first."""
    record = logging.LogRecord("t", logging.ERROR, "f.py", 1, "failed", None, None)
    record.exc_text = f"Traceback: contact {CANDIDATE['name']} unreachable"
    IdentityRedactionFilter({CANDIDATE["name"]}).filter(record)
    assert CANDIDATE["name"] not in (record.exc_text or "")


# --- the connection logging setup opens (spec 076) ---------------------------


def _wal_files(path: Path) -> set[str]:
    return {
        sibling.name
        for sibling in path.parent.iterdir()
        if sibling.name in {f"{path.name}-wal", f"{path.name}-shm"}
    }


@pytest.fixture()
def quiet_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A WAL database nothing holds open, with gc off.

    gc is off because an unclosed connection is closed, and the WAL
    checkpointed, when it is collected; with gc on, a collection between the
    call and the assertion would pass a leak.
    """
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    conn = connect()
    path = Path(conn.execute("PRAGMA database_list").fetchone()["file"])
    conn.close()
    assert _wal_files(path) == set()
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield path
    finally:
        if was_enabled:
            gc.enable()


def test_configure_logging_closes_its_connection(quiet_database: Path) -> None:
    configure_logging(force=True)
    assert _wal_files(quiet_database) == set()


def test_configure_logging_closes_its_connection_when_identity_values_fails(
    quiet_database: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import harrier.logredact

    def broken(_conn: Connection) -> set[str]:
        raise StoreDatabaseError("unreadable")

    monkeypatch.setattr(harrier.logredact, "identity_values", broken)
    # configure_logging replaces the root handlers, caplog's included, so the
    # warning is read from the stderr handler it installs.
    configure_logging(force=True)
    assert _wal_files(quiet_database) == set()
    assert "identity redaction is unavailable" in capsys.readouterr().err


# --- the store host and user (spec 145) ---
#
# psycopg logs its own connection, transaction and pipeline objects as
# arguments when a rollback fails (`psycopg/transaction.py`), and their repr
# names the host, the port and the user. `harrier store` configures no
# logging, so such a line goes through the last-resort handler.

DATABASE_URL = "HARRIER_DATABASE_URL"
IMPORT_URL = "HARRIER_IMPORT_DATABASE_URL"
ACCOUNT_URL = "HARRIER_ACCOUNT_DATABASE_URL"

# Synthetic stores, one per variable, each with a pooler-shaped user.
STORES = {
    DATABASE_URL: ("db.store-one.example", "postgres.storeoneref"),
    IMPORT_URL: ("db.store-two.example", "postgres.storetworef"),
    ACCOUNT_URL: ("db.store-three.example", "postgres.storethreeref"),
}


def _store_url(host: str, user: str, port: str = "6543") -> str:
    return f"postgresql://{user}:synthetic-pw@{host}:{port}/postgres"


def _stores(monkeypatch: pytest.MonkeyPatch, **urls: str) -> None:
    """Set the three store variables: each named one to its URL, the rest
    empty or absent. `HARRIER_DATABASE_URL` stays set, as the test pin
    requires; an empty value is the local store."""
    monkeypatch.setenv(DATABASE_URL, urls.get(DATABASE_URL, ""))
    for variable in (IMPORT_URL, ACCOUNT_URL):
        if variable in urls:
            monkeypatch.setenv(variable, urls[variable])
        else:
            monkeypatch.delenv(variable, raising=False)


class _Repr:
    """An object that prints as psycopg's own objects print."""

    def __init__(self, text: str) -> None:
        self._text = text

    def __repr__(self) -> str:
        return self._text


def _transaction(host: str, user: str, port: str = "6543") -> _Repr:
    """A psycopg transaction as psycopg itself prints one: its repr built by
    `connection_summary`, on a connection libpq reports as OK."""
    from types import SimpleNamespace
    from typing import Any, cast

    from psycopg.pq import ConnStatus, TransactionStatus
    from psycopg.pq.misc import connection_summary

    pgconn = SimpleNamespace(
        status=ConnStatus.OK,
        transaction_status=TransactionStatus.INERROR,
        pipeline_status=0,
        host=host.encode(),
        port=port.encode(),
        user=user.encode(),
        db=b"postgres",
    )
    summary = connection_summary(cast(Any, pgconn))
    return _Repr(f"<psycopg.Transaction (ACTIVE) {summary} at 0x1>")


def _rollback_failed(transaction: _Repr) -> None:
    """The line psycopg logs when a rollback fails (`psycopg/transaction.py`)."""
    from psycopg import OperationalError

    logging.getLogger("psycopg").warning(
        "error ignored in rollback of %s: %s",
        transaction,
        OperationalError("consuming input failed"),
    )


def _last_resort(monkeypatch: pytest.MonkeyPatch) -> None:
    """A process that configured no logging, as `harrier store` is, with the
    scrub the CLI installs before any command runs. Called in the test body:
    pytest adds its own handler to the root when the test starts."""
    from harrier.logsetup import scrub_last_resort

    handler = logging.lastResort
    assert handler is not None
    monkeypatch.setattr(handler, "filters", [])
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    scrub_last_resort()


def test_the_last_resort_masks_the_store_host(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _last_resort(monkeypatch)
    host, user = STORES[DATABASE_URL]
    _stores(monkeypatch, **{DATABASE_URL: _store_url(host, user)})

    _rollback_failed(_transaction(host, user))

    written = capsys.readouterr().err
    assert "error ignored in rollback of" in written
    assert host not in written
    assert "host=REDACTED port=6543" in written


@pytest.mark.parametrize("variable", [IMPORT_URL, ACCOUNT_URL])
def test_each_store_url_host_is_masked(
    variable: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _last_resort(monkeypatch)
    host, user = STORES[variable]
    _stores(monkeypatch, **{variable: _store_url(host, user)})

    _rollback_failed(_transaction(host, user))

    written = capsys.readouterr().err
    assert "error ignored in rollback of" in written
    assert host not in written
    assert "host=REDACTED" in written


@pytest.mark.dotenv_fallback
def test_the_host_variables_are_read_where_their_specs_read_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`HARRIER_DATABASE_URL` is read as `store_target` reads it, `.env`
    included. The import and account URLs are read from the environment
    only, as specs 141 and 144 read them."""
    _last_resort(monkeypatch)
    _stores(monkeypatch)
    # Into tmp_path before the variable goes, so no other `.env` is read.
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "".join(f"{variable}={_store_url(*STORES[variable])}\n" for variable in STORES),
        encoding="utf-8",
    )
    monkeypatch.delenv(DATABASE_URL)

    hosts = " ".join(host for host, _ in STORES.values())
    logging.getLogger("harrier.tests.hosts").warning("hosts: %s", hosts)

    written = capsys.readouterr().err
    assert STORES[DATABASE_URL][0] not in written
    assert STORES[IMPORT_URL][0] in written
    assert STORES[ACCOUNT_URL][0] in written


def test_the_store_user_is_masked_after_user_equals(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only after `user=`, so a plain `postgres` user does not mask that word
    wherever a message uses it."""
    _last_resort(monkeypatch)
    direct = "db.direct.example"
    _stores(
        monkeypatch,
        **{
            DATABASE_URL: _store_url(*STORES[DATABASE_URL]),
            IMPORT_URL: _store_url(*STORES[IMPORT_URL]),
            # A direct connection, whose user is the plain role name.
            ACCOUNT_URL: _store_url(direct, "postgres"),
        },
    )
    one, two = STORES[DATABASE_URL][1], STORES[IMPORT_URL][1]

    # The longer name in the format string: an argument that is the whole
    # user is masked on its own, whatever follows it (spec 145, failure modes).
    logging.getLogger("psycopg").warning(
        f"users: (user=%s) user='%s' user=postgres, the postgres role, user=stranger, user={one}x",
        one,
        two,
    )

    written = capsys.readouterr().err
    assert one not in written.replace(f"user={one}x", "")
    assert two not in written
    # `postgres` names no project, so it is never masked (spec 145, amendment).
    assert "(user=REDACTED) user='REDACTED' user=postgres, the postgres role" in written
    assert "user=stranger" in written
    assert f"user={one}x" in written

    # Bare before a closing quote, as a keyword conninfo in a repr reads,
    # and at the end of the text. Matching is exact, as role names are.
    logging.getLogger("psycopg").warning(
        "conninfo 'host=h user=%s' upper user=%s last user=%s", two, one.upper(), one
    )

    written = capsys.readouterr().err
    assert (
        f"conninfo 'host=h user=REDACTED' upper user={one.upper()} last user=REDACTED\n" in written
    )


def test_the_store_user_is_masked_in_a_url_and_at_every_ending(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """On Supabase's shared pooler the host is regional, so in a logged URL
    the user is what names the project (spec 145, amendment)."""
    _last_resort(monkeypatch)
    host, one = STORES[DATABASE_URL]
    encoded_host = STORES[IMPORT_URL][0]
    encoded = "harrier%40ops"
    _stores(
        monkeypatch,
        **{
            DATABASE_URL: _store_url(host, one),
            # A user libpq reads as `harrier@ops`, written percent-encoded.
            IMPORT_URL: _store_url(encoded_host, encoded),
            # Encoded only in part, as `encodeURIComponent` writes it: the
            # form as written is neither the decoded nor `quote`'s.
            ACCOUNT_URL: _store_url(STORES[ACCOUNT_URL][0], "ops%3Ab(c)*"),
        },
    )
    log = logging.getLogger("harrier.tests.users")

    log.warning("opening %s", _store_url(host, one))
    log.warning("opening %s", _store_url(encoded_host, encoded))
    log.warning("opening %s", _store_url(STORES[ACCOUNT_URL][0], "ops%3Ab(c)*"))
    log.warning("json %s", f"postgresql:\\/\\/{one}@{host}")
    log.warning(
        f'endings ?user=%s&sslmode=require user=%s; user=%s. user="%s" user={one}.more',
        one,
        one,
        one,
        one,
    )

    written = capsys.readouterr().err
    assert "opening postgresql://REDACTED:REDACTED@REDACTED:6543/postgres\n" in written
    assert written.count("opening postgresql://REDACTED:REDACTED@REDACTED:6543/postgres") == 3
    assert "json postgresql:\\/\\/REDACTED@REDACTED\n" in written
    assert (
        "endings ?user=REDACTED&sslmode=require user=REDACTED; user=REDACTED."
        f' user="REDACTED" user={one}.more\n' in written
    )
    assert encoded not in written


def test_the_store_user_is_masked_wherever_it_stands_as_a_role_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every shape the local privacy review of PR #360 found printing the
    user, and the longer role names that must stay (spec 145, amendment)."""
    import json

    _last_resort(monkeypatch)
    host, one = STORES[DATABASE_URL]
    _stores(monkeypatch, **{DATABASE_URL: _store_url(host, one)})
    log = logging.getLogger("harrier.tests.users")
    shapes = {
        "dict": (str({"user": one, "host": host}), "{'user': 'REDACTED', 'host': 'REDACTED'}"),
        "json": (json.dumps({"user": one}), '{"user": "REDACTED"}'),
        "keyword": (f"user = '{one}' host = {host}", "user = 'REDACTED' host = REDACTED"),
        "escaped": (f'user=\\"{one}\\"', 'user=\\"REDACTED\\"'),
        "no scheme": (f"connecting as {one}@{host}:6543", "connecting as REDACTED@REDACTED:6543"),
        "encoded": (
            f"next=postgresql%3A%2F%2F{one}%3Asynthetic-pw%40{host}",
            "next=postgresql%3A%2F%2FREDACTED%3AREDACTED%40REDACTED",
        ),
        "libpq": (
            f'password authentication failed for user "{one}"',
            'password authentication failed for user "REDACTED"',
        ),
        "longer": (f"x.{one} {one}x {one}.more harrier_account", None),
    }

    for name, (text, _) in shapes.items():
        log.warning("%s: %s", name, text)

    written = capsys.readouterr().err
    for name, (text, expected) in shapes.items():
        assert f"{name}: {expected if expected is not None else text}\n" in written, name


def test_a_non_ascii_user_and_host_are_masked_as_json_and_repr_write_them(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`json.dumps` writes a non-ASCII character as a `\\u` escape and `repr`
    may escape it too, as the password forms already allow for (spec 133);
    the user and the host are masked in those forms as well (CodeRabbit
    review of PR #360)."""
    import json

    _last_resort(monkeypatch)
    user, host = "café.ref", "db.café.example"
    _stores(
        monkeypatch,
        **{DATABASE_URL: f"postgresql://caf%C3%A9.ref:synthetic-pw@{host}:6543/postgres"},
    )
    log = logging.getLogger("harrier.tests.users")

    log.warning("json %s", json.dumps({"user": user, "host": host}))
    log.warning("repr %r", {"user": user, "host": host})
    log.warning("plain %s %s", user, host)

    written = capsys.readouterr().err
    assert 'json {"user": "REDACTED", "host": "REDACTED"}\n' in written
    assert "repr {'user': 'REDACTED', 'host': 'REDACTED'}\n" in written
    assert "plain REDACTED REDACTED\n" in written


def test_a_non_ascii_user_and_host_are_masked_as_ascii_writes_them(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ascii()` and `%a` write a non-ASCII character as a `\\x` escape, so
    the user and the host are masked in that form too, and a host in any
    case (spec 133, Amendment (2026-10-15): a password quoted by `ascii()`).
    `scrub_outbound` still keeps the host (spec 145)."""
    from harrier.logredact import scrub_outbound

    _last_resort(monkeypatch)
    user, host = "café.ref", "db.café.example"
    _stores(
        monkeypatch,
        **{DATABASE_URL: f"postgresql://caf%C3%A9.ref:synthetic-pw@{host}:6543/postgres"},
    )
    log = logging.getLogger("harrier.tests.users")
    assert ascii(user) == "'caf\\xe9.ref'", "ascii() escaped nothing"

    log.warning("ascii %s", ascii({"user": user, "host": host}))
    log.warning("percent-a %a %a", user, host)
    log.warning("upper %a", "DB.CAFé.EXAMPLE")

    written = capsys.readouterr().err
    assert "ascii {'user': 'REDACTED', 'host': 'REDACTED'}\n" in written
    assert "percent-a 'REDACTED' 'REDACTED'\n" in written
    assert "upper 'REDACTED'\n" in written
    assert "caf\\xe9" not in written.lower()
    text = f"cannot connect to the Postgres store at {host!a}"
    assert scrub_outbound(text) == text


def test_a_configured_handler_masks_the_store_host(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    host, user = STORES[DATABASE_URL]
    written = _capture(monkeypatch)
    # Set after logging is configured, which runs on the local store; the
    # scrub reads the URL when a record is logged.
    _stores(monkeypatch, **{DATABASE_URL: _store_url(host, user)})

    _rollback_failed(_transaction(host, user))

    assert len(written) == 1, "nothing was logged"
    line = written[0]
    assert "error ignored in rollback of" in line
    assert host not in line
    assert user not in line
    assert "host=REDACTED port=6543 user=REDACTED" in line


def test_the_host_mask_keeps_its_boundaries(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _last_resort(monkeypatch)
    _stores(
        monkeypatch,
        **{
            DATABASE_URL: _store_url("db.example.co", "harrier"),
            IMPORT_URL: _store_url("localhost", "harrier"),
            ACCOUNT_URL: "postgresql://harrier@/postgres?host=/var/run/postgresql",
        },
    )

    logging.getLogger("harrier.tests.hosts").warning(
        "seen: db.example.com, DB.EXAMPLE.CO, localhost and db.example.co."
    )
    # A longer name on either side, with a `.` or a `-`, and a socket
    # directory, which names no server.
    logging.getLogger("harrier.tests.hosts").warning(
        "kept: sub.db.example.co x-db.example.co db.example.co.uk db.example.co-x"
        " /var/run/postgresql"
    )

    written = capsys.readouterr().err
    assert "seen: db.example.com, REDACTED, localhost and REDACTED." in written
    assert (
        "kept: sub.db.example.co x-db.example.co db.example.co.uk db.example.co-x"
        " /var/run/postgresql" in written
    )


def test_a_host_after_an_escape_is_masked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The letter that ends a written-out `\\n`, and the digit that ends
    `%40`, are not part of the host (spec 145, amendment)."""
    _last_resort(monkeypatch)
    host, user = STORES[DATABASE_URL]
    _stores(monkeypatch, **{DATABASE_URL: _store_url(host, user)})
    log = logging.getLogger("harrier.tests.hosts")

    log.warning("repr %r", f"connect failed:\n{host} unreachable")
    log.warning("encoded next=postgresql%%3A%%2F%%2Fops%%40%s%%3A6543", host)
    # Escapes longer than a letter: `\\x1b` (a terminal colour code), `\\xa0`
    # and `\\u200b` as `repr` writes them, and `\\u00a0` as JSON writes it.
    log.warning("hex %r", f"x\x1b{host}")
    log.warning("nbsp %r", f"x\xa0{host}")
    log.warning("zwsp %r", f"x\u200b{host}")
    log.warning("json %s", f'"x\\u00a0{host}"')

    written = capsys.readouterr().err
    assert "repr 'connect failed:\\nREDACTED unreachable'" in written
    assert "encoded next=postgresql%3A%2F%2Fops%40REDACTED%3A6543" in written
    assert "hex 'x\\x1bREDACTED'" in written
    assert "nbsp 'x\\xa0REDACTED'" in written
    assert "zwsp 'x\\u200bREDACTED'" in written
    assert 'json "x\\u00a0REDACTED"' in written
    assert host not in written


def test_every_host_of_a_multi_host_url_is_masked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _last_resort(monkeypatch)
    hosts = ("db-a.store.example", "db-b.store.example", "192.0.2.10", "192.0.2.11")
    url = (
        f"postgresql://harrier:synthetic-pw@{hosts[0]}:5432,{hosts[1]}:5433/postgres"
        f"?hostaddr={hosts[2]},{hosts[3]}"
    )
    _stores(monkeypatch, **{DATABASE_URL: url})

    logging.getLogger("harrier.tests.hosts").warning("hosts: %s", ", ".join(hosts))

    written = capsys.readouterr().err
    assert "hosts: REDACTED, REDACTED, REDACTED, REDACTED" in written


def test_a_store_url_is_read_as_libpq_reads_it_before_the_driver_loads(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A record logged before psycopg is loaded, as on the local store
    during `harrier store import-local`, reads a multi-host URL as libpq
    does. The reading is cached by URL, so one taken with `urlsplit` would
    leave every host after the first unmasked once the driver was loaded
    (privacy review of PR #360)."""
    import sys

    from harrier.logredact import (
        _hosts_and_users_of,  # pyright: ignore[reportPrivateUsage]
    )

    _last_resort(monkeypatch)
    hosts = ("db-a.store.example", "db-b.store.example")
    url = f"postgresql://postgres.importref:synthetic-pw@{hosts[0]}:5432,{hosts[1]}:5432/postgres"
    _stores(monkeypatch, **{IMPORT_URL: url})
    _hosts_and_users_of.cache_clear()
    monkeypatch.delitem(sys.modules, "psycopg.conninfo", raising=False)
    try:
        logging.getLogger("harrier.tests.hosts").warning("hosts: %s", ", ".join(hosts))
    finally:
        _hosts_and_users_of.cache_clear()

    written = capsys.readouterr().err
    assert "hosts: REDACTED, REDACTED" in written


def test_a_url_libpq_cannot_read_is_read_with_urlsplit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from harrier.logredact import (
        _hosts_and_users_of,  # pyright: ignore[reportPrivateUsage]
    )

    _last_resort(monkeypatch)
    host, user = STORES[DATABASE_URL]
    _stores(monkeypatch, **{DATABASE_URL: _store_url(host, user)})

    def unreadable(_url: str) -> dict[str, object] | None:
        return None

    monkeypatch.setattr("harrier.logredact._libpq_reading", unreadable)
    _hosts_and_users_of.cache_clear()
    try:
        _rollback_failed(_transaction(host, user))
    finally:
        _hosts_and_users_of.cache_clear()

    written = capsys.readouterr().err
    assert "host=REDACTED port=6543 user=REDACTED" in written


def test_a_format_that_does_not_fit_its_arguments_masks_the_host(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The record keeps its format string, scrubbed, and withholds the
    arguments (spec 133); the host in the format string is masked too."""
    _last_resort(monkeypatch)
    host, user = STORES[DATABASE_URL]
    _stores(monkeypatch, **{DATABASE_URL: _store_url(host, user)})

    logging.getLogger("harrier.tests.hosts").warning(f"connecting to {host} as %s %s", "one")

    written = capsys.readouterr().err
    assert "connecting to REDACTED as %s %s (arguments withheld" in written
    assert host not in written


def test_a_cancel_failure_prints_no_host(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The route the privacy review of PR #349 named: a cancel failure is
    logged with the driver's error, whose libpq text names the host and its
    address. Spec 135 prints a driver error as the seam's text, so neither
    appears."""
    _last_resort(monkeypatch)
    from psycopg import OperationalError

    host, user = STORES[DATABASE_URL]
    _stores(monkeypatch, **{DATABASE_URL: _store_url(host, user)})
    error = OperationalError(
        f'connection to server at "{host}" (203.0.113.7), port 6543 failed: timeout expired'
    )

    logging.getLogger("psycopg").warning("query cancellation failed: %s", error)

    written = capsys.readouterr().err
    assert "query cancellation failed: OperationalError\n" in written
    assert host not in written
    assert "203.0.113.7" not in written


def test_scrub_outbound_keeps_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP error bodies and run events name the store they refused on
    purpose (spec 103); only log records mask the host."""
    from harrier.logredact import scrub_outbound

    host, user = STORES[DATABASE_URL]
    _stores(monkeypatch, **{DATABASE_URL: _store_url(host, user)})
    text = f"cannot connect to the Postgres store at {host}:6543/postgres: user={user}"

    assert scrub_outbound(text) == text
