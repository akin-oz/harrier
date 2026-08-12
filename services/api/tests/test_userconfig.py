"""User configuration in the database (spec 023, ADR-009).

Pyright strict cannot resolve starlette's TestClient request and response
types, so the three unknown-type rules are off for this file, as they are in
test_api_jobs.py.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from conftest import auth
from fastapi.testclient import TestClient

from harrier.db import connect
from harrier.discovery import scheduled_apify_count
from harrier.userconfig import (
    COMPANY_HOLDS,
    DISCOVERY,
    FEEDS,
    KINDS,
    ConfigError,
    delete_config,
    get_config,
    list_config,
    load_ats_feeds,
    load_discovery_settings,
    load_feed_urls,
    load_hold_companies,
    load_search_urls,
    set_config,
)
from harrier_api.app import create_app
from harrier_cli.main import main

EXAMPLE_FEEDS = ["https://boards.greenhouse.io/exampleco", "https://jobs.ashbyhq.com/exampleco"]


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    # No config/ tree in the working directory, so a file fallback that
    # resolves has to be one this test put there on purpose.
    monkeypatch.chdir(tmp_path)
    return connect()


# --- the store ---------------------------------------------------------------


def test_a_stored_value_round_trips(db: sqlite3.Connection) -> None:
    set_config(db, FEEDS, EXAMPLE_FEEDS)
    assert get_config(db, FEEDS) == EXAMPLE_FEEDS
    assert load_feed_urls(db) == EXAMPLE_FEEDS


def test_the_schema_carries_no_scope_column(db: sqlite3.Connection) -> None:
    """`scope` is gone (spec 041).

    It never held anything but 'default', it was threaded through eight
    signatures, and it guarded the one table with no personal data while the
    tables that hold it had no equivalent. ADR-009 wanted a tenancy seam and
    this was not one; it now records that re-adding it is a migration.
    """
    columns = {row[1] for row in db.execute("PRAGMA table_info(user_config)")}
    assert "scope" not in columns
    assert {"kind", "value", "updated_at"} <= columns


def test_a_kind_is_unique_on_its_own(db: sqlite3.Connection) -> None:
    """The uniqueness that used to be (scope, kind). Setting a kind twice
    updates rather than adding a second row."""
    set_config(db, FEEDS, ["https://boards.greenhouse.io/one"])
    set_config(db, FEEDS, ["https://boards.greenhouse.io/two"])
    rows = list(db.execute("SELECT COUNT(*) FROM user_config WHERE kind = ?", (FEEDS,)))
    assert rows[0][0] == 1
    assert load_feed_urls(db) == ["https://boards.greenhouse.io/two"]


def test_setting_the_same_kind_twice_updates_rather_than_duplicates(
    db: sqlite3.Connection,
) -> None:
    set_config(db, FEEDS, EXAMPLE_FEEDS)
    set_config(db, FEEDS, ["https://jobs.lever.co/exampleco"])
    assert len(list_config(db)) == 1
    assert load_feed_urls(db) == ["https://jobs.lever.co/exampleco"]


def test_an_empty_list_is_not_the_same_as_no_row(db: sqlite3.Connection, tmp_path: Path) -> None:
    """Clearing the watchlist has to mean something different from never
    having set one, or a user who empties it gets the file back."""
    config = tmp_path / "config"
    config.mkdir()
    (config / "feeds.txt").write_text("https://boards.greenhouse.io/from-file\n", encoding="utf-8")
    assert load_feed_urls(db) == ["https://boards.greenhouse.io/from-file"]

    set_config(db, FEEDS, [])
    assert load_feed_urls(db) == []

    delete_config(db, FEEDS)
    assert load_feed_urls(db) == ["https://boards.greenhouse.io/from-file"]


def test_a_bad_shape_is_refused_at_the_write(db: sqlite3.Connection) -> None:
    # Validating on read would surface a bad value inside discovery, far
    # from whoever set it.
    with pytest.raises(ConfigError, match="must be a JSON list"):
        set_config(db, FEEDS, {"not": "a list"})
    with pytest.raises(ConfigError, match="must be a JSON object"):
        set_config(db, DISCOVERY, ["not an object"])
    with pytest.raises(ConfigError, match="entries must be strings"):
        set_config(db, FEEDS, ["fine", 7])
    with pytest.raises(ConfigError, match="unknown configuration kind"):
        set_config(db, "nonsense", [])


def test_blank_entries_are_dropped_on_the_way_in(db: sqlite3.Connection) -> None:
    set_config(db, FEEDS, ["  https://boards.greenhouse.io/exampleco  ", "", "   "])
    assert load_feed_urls(db) == ["https://boards.greenhouse.io/exampleco"]


# --- resolution order --------------------------------------------------------


def test_a_fresh_install_with_no_store_and_no_files_runs_with_no_sources(
    db: sqlite3.Connection,
) -> None:
    """The spec's acceptance criterion: this is a clean state, not an error."""
    assert load_feed_urls(db) == []
    assert load_search_urls(db) == []
    assert load_hold_companies(db) == set()
    assert load_discovery_settings(db) == {}
    assert load_ats_feeds(db) == {"greenhouse": [], "ashby": [], "lever": [], "unrouted": []}


def test_the_file_is_used_until_something_is_stored(db: sqlite3.Connection, tmp_path: Path) -> None:
    """An existing install keeps working before `harrier config import` runs."""
    config = tmp_path / "config"
    config.mkdir()
    (config / "feeds.txt").write_text(
        "# a comment\nhttps://boards.greenhouse.io/from-file\n", encoding="utf-8"
    )
    assert load_feed_urls(db) == ["https://boards.greenhouse.io/from-file"]
    set_config(db, FEEDS, EXAMPLE_FEEDS)
    assert load_feed_urls(db) == EXAMPLE_FEEDS


def test_stored_feeds_route_to_their_importers(db: sqlite3.Connection) -> None:
    set_config(db, FEEDS, [*EXAMPLE_FEEDS, "https://jobs.eu.lever.co/example-eu-co"])
    grouped = load_ats_feeds(db)
    assert grouped["greenhouse"] == ["https://boards.greenhouse.io/exampleco"]
    assert grouped["ashby"] == ["https://jobs.ashbyhq.com/exampleco"]
    assert grouped["lever"] == ["https://jobs.eu.lever.co/example-eu-co"]


def test_hold_companies_are_normalized_from_the_store(db: sqlite3.Connection) -> None:
    set_config(db, COMPANY_HOLDS, ["Example Co", "  Other Co  "])
    assert load_hold_companies(db) == {"example co", "other co"}


def test_discovery_settings_come_from_the_store(db: sqlite3.Connection) -> None:
    set_config(db, DISCOVERY, {"apify_scheduled_count": 50})
    assert scheduled_apify_count(conn=db) == 50
    delete_config(db, DISCOVERY)
    # Falls back to the CLI default when neither store nor file has a value.
    assert scheduled_apify_count(conn=db) == 150


def test_a_boolean_count_does_not_pass_as_an_integer(db: sqlite3.Connection) -> None:
    # JSON true satisfies isinstance(x, int); it must not become a count.
    set_config(db, DISCOVERY, {"apify_scheduled_count": True})
    assert scheduled_apify_count(conn=db) == 150


def test_accessors_work_without_a_connection(db: sqlite3.Connection) -> None:
    # None means "no store here", which is how file-based callers and every
    # test predating spec 023 keep working unchanged.
    assert load_feed_urls(None) == []
    assert load_hold_companies() == set()


def test_stored_json_that_is_not_a_list_is_reported(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO user_config (kind, value) VALUES (?, ?)",
        (FEEDS, json.dumps({"unexpected": True})),
    )
    db.commit()
    # One validator, so the read path reports exactly what the write path
    # would have refused.
    with pytest.raises(ConfigError, match="must be a JSON list"):
        load_feed_urls(db)


# --- the CLI -----------------------------------------------------------------


def test_import_round_trips_the_current_files(db: sqlite3.Connection, tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "feeds.txt").write_text("\n".join(EXAMPLE_FEEDS) + "\n", encoding="utf-8")
    (config / "linkedin_search_urls.txt").write_text(
        "https://www.linkedin.com/jobs/search/?keywords=example\n", encoding="utf-8"
    )
    (config / "companies-hold.csv").write_text(
        "company,reason\nExample Co,not a fit\n", encoding="utf-8"
    )
    (config / "discovery.json").write_text(
        json.dumps({"_comment": "explains the file", "apify_scheduled_count": 50}), encoding="utf-8"
    )

    assert main(["config", "import"]) == 0

    fresh = connect()
    try:
        assert load_feed_urls(fresh) == EXAMPLE_FEEDS
        assert load_search_urls(fresh) == ["https://www.linkedin.com/jobs/search/?keywords=example"]
        assert load_hold_companies(fresh) == {"example co"}
        # The example file's reader-facing _comment is not a setting.
        assert load_discovery_settings(fresh) == {"apify_scheduled_count": 50}
    finally:
        fresh.close()


def test_import_with_no_files_reports_rather_than_claiming_success(db: sqlite3.Connection) -> None:
    assert main(["config", "import"]) == 1


def test_unset_reports_whether_anything_was_removed(db: sqlite3.Connection) -> None:
    set_config(db, FEEDS, EXAMPLE_FEEDS)
    db.commit()
    assert main(["config", "unset", "feeds"]) == 0
    assert main(["config", "unset", "feeds"]) == 1


def test_get_on_an_unstored_kind_exits_non_zero(db: sqlite3.Connection) -> None:
    assert main(["config", "get", "feeds"]) == 1


# --- the API -----------------------------------------------------------------


@pytest.fixture()
def client(db: sqlite3.Connection) -> TestClient:
    return TestClient(create_app())


def test_the_api_lists_every_kind_with_its_source(client: TestClient) -> None:
    body = client.get("/config").json()
    assert {entry["kind"] for entry in body} == set(KINDS)
    # Nothing stored yet, so every value is still coming from a file.
    assert {entry["source"] for entry in body} == {"file"}


def test_putting_a_value_makes_it_the_stored_source(client: TestClient) -> None:
    response = client.put("/config/feeds", json={"value": EXAMPLE_FEEDS}, headers=auth())
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "store"
    assert body["value"] == EXAMPLE_FEEDS
    assert client.get("/config/feeds").json()["value"] == EXAMPLE_FEEDS


def test_deleting_a_value_restores_the_fallback(client: TestClient) -> None:
    client.put("/config/feeds", json={"value": EXAMPLE_FEEDS}, headers=auth())
    body = client.delete("/config/feeds", headers=auth()).json()
    assert body["source"] == "file"
    assert body["value"] == []


def test_the_api_refuses_a_bad_shape_with_the_stores_own_message(client: TestClient) -> None:
    # The shape rules live in the store, so the API cannot drift from the CLI.
    response = client.put("/config/feeds", json={"value": {"not": "a list"}}, headers=auth())
    assert response.status_code == 400
    assert "must be a JSON list" in response.json()["detail"]


def test_a_malformed_body_and_a_bad_value_are_different_failures(client: TestClient) -> None:
    """FastAPI owns 422 for request validation, where detail is a list of
    field errors. Store validation answers 400 with a sentence, so a client
    can tell "you sent nonsense" from "that value is wrong for this kind"
    (review finding on PR #20)."""
    malformed = client.put("/config/feeds", json={"wrong_field": []}, headers=auth())
    assert malformed.status_code == 422
    assert isinstance(malformed.json()["detail"], list)

    bad_value = client.put("/config/feeds", json={"value": 7}, headers=auth())
    assert bad_value.status_code == 400
    assert isinstance(bad_value.json()["detail"], str)


def test_a_corrupted_row_is_refused_rather_than_coerced(
    client: TestClient, db: sqlite3.Connection
) -> None:
    """A row can appear without going through set_config: a hand-edited
    database, a restored backup, a future migration. The read path was
    coercing [7] into ["7"] (review finding on PR #20)."""
    db.execute(
        "INSERT INTO user_config (kind, value) VALUES (?, ?)",
        (FEEDS, json.dumps([7])),
    )
    db.commit()
    with pytest.raises(ConfigError, match="entries must be strings"):
        load_feed_urls(db)


def test_an_unknown_kind_is_a_404_on_every_verb(client: TestClient) -> None:
    assert client.get("/config/nonsense").status_code == 404
    assert client.put("/config/nonsense", json={"value": []}, headers=auth()).status_code == 404
    assert client.delete("/config/nonsense", headers=auth()).status_code == 404


def test_every_migration_version_is_unique_and_ordered() -> None:
    """Two migrations numbered the same means one never runs.

    The runner skips any version at or below the recorded one, so a
    duplicate is not an error, it is a silent omission that shows up later
    as a missing column. Three open branches each added a migration 4 at
    once, which is how this was found; the branch numbers are now 4, 5 and 6
    and this stops the next collision being discovered in production.
    """
    from harrier.tracker.schema import MIGRATIONS

    versions = [version for version, _ in MIGRATIONS]
    assert versions == sorted(versions), f"migrations are out of order: {versions}"
    assert len(versions) == len(set(versions)), f"duplicate migration version in {versions}"
    assert versions[0] == 1, "migrations start at 1"
