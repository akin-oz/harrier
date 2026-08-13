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

import logging
import sqlite3
from pathlib import Path

import pytest

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
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    conn = connect()
    import json

    conn.execute(
        "INSERT INTO profile_documents (kind, name, format, content) VALUES (?, ?, ?, ?)",
        ("resume_data", "bundle", "json", json.dumps({"candidate": CANDIDATE})),
    )
    conn.execute(
        "INSERT INTO contacts (person_name, person_email) VALUES (?, ?)",
        ("Jordan Sample", "jordan@example.com"),
    )
    conn.commit()
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


def test_identity_values_reads_candidate_and_contacts(db: sqlite3.Connection) -> None:
    values = identity_values(db)
    assert CANDIDATE["name"] in values
    assert CANDIDATE["email"] in values
    assert "Jordan Sample" in values
    assert "jordan@example.com" in values


def test_identity_values_survives_a_database_without_the_tables(tmp_path: Path) -> None:
    """A fresh clone has no profile store. Redaction must degrade, not raise."""
    bare = sqlite3.connect(tmp_path / "bare.db")
    assert identity_values(bare) == set()


def test_short_values_are_not_redactable(tmp_path: Path) -> None:
    """A one-letter name would shred every unrelated line that contains it."""
    conn = sqlite3.connect(tmp_path / "short.db")
    conn.execute("CREATE TABLE contacts (person_name TEXT, person_email TEXT, linkedin_url TEXT)")
    conn.execute("INSERT INTO contacts VALUES ('Jo', '', '')")
    conn.commit()
    assert identity_values(conn) == set()


def test_the_candidate_name_does_not_reach_a_log_line(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = _capture(monkeypatch)
    logging.getLogger("harrier.somewhere").warning("draft prepared for %s", CANDIDATE["name"])
    assert written, "nothing was logged"
    assert CANDIDATE["name"] not in written[0]
    assert REDACTION in written[0]


def test_a_contact_address_does_not_reach_a_log_line(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = _capture(monkeypatch)
    logging.getLogger("harrier.outreach").info("queued jordan@example.com")
    assert written, "nothing was logged"
    assert "jordan@example.com" not in written[0]


def test_an_unrelated_line_is_left_alone(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = _capture(monkeypatch)
    logging.getLogger("harrier.discovery").info("fetched 15 postings from exampleco")
    assert written[0] == "fetched 15 postings from exampleco"


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
