"""Demo mode: offline, deterministic, and free of anyone real (spec 021).

The privacy pass at the bottom is the mechanical half of the pre-publish
checklist in docs/privacy-plan.md: it reads every committed fixture and
example and fails on any host, path, or address that could name a real
company or person.

Pyright strict cannot resolve starlette's TestClient request and response
types (they surface as Unknown through httpx private aliases), so the three
unknown-type rules are off for this file, as they are in test_api_jobs.py.
Everything else here is annotated rather than suppressed.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from harrier.db import connect, data_dir
from harrier.demo import (
    OfflineFixtureError,
    demo_data_dir,
    example_path_for,
    http_fixtures_dir,
    repo_root,
    resolve_config_path,
)
from harrier.discovery import DiscoveryOptions, run_discovery
from harrier.screening.http import request_text
from harrier.sources.feeds import parse_ats_feeds
from harrier_api.app import create_app
from harrier_api.demo import PROFILE_SEEDS, demo_db_path, seed_demo_db, seed_profile_documents

ROOT = repo_root()


@pytest.fixture()
def demo_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HARRIER_DEMO", "1")
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def test_demo_mode_reads_the_committed_example_even_when_a_real_config_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "feeds.txt"
    real.write_text("https://boards.greenhouse.io/private-watchlist\n", encoding="utf-8")
    example_path_for(real).write_text("https://boards.greenhouse.io/exampleco\n", encoding="utf-8")

    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    assert resolve_config_path(real) == real

    monkeypatch.setenv("HARRIER_DEMO", "1")
    # The owner's own watchlist is personal data: the example has to win even
    # though the real file is right there (ADR-009).
    assert resolve_config_path(real) == example_path_for(real)


def test_demo_feeds_resolve_from_the_repo_regardless_of_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARRIER_DEMO", "1")
    monkeypatch.chdir(tmp_path)
    feeds = parse_ats_feeds()
    assert feeds["greenhouse"] and feeds["ashby"] and feeds["lever"]


def test_demo_writes_nothing_into_the_clone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARRIER_DEMO", "1")
    monkeypatch.delenv("HARRIER_DATA_DIR", raising=False)
    target = data_dir().resolve()
    assert target == demo_data_dir().resolve()
    assert not target.is_relative_to(ROOT)


def test_unfixtured_url_raises_instead_of_reaching_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "index.json").write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setenv("HARRIER_HTTP_FIXTURES", str(tmp_path))
    with pytest.raises(OfflineFixtureError) as excinfo:
        request_text("https://boards-api.greenhouse.io/v1/boards/real-company/jobs")
    assert "real-company" in str(excinfo.value)


def test_fixture_entry_cannot_escape_the_fixture_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://example.test/thing"
    (tmp_path / "index.json").write_text(json.dumps({url: "../outside.json"}), encoding="utf-8")
    (tmp_path.parent / "outside.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HARRIER_HTTP_FIXTURES", str(tmp_path))
    with pytest.raises(OfflineFixtureError, match="plain filename"):
        request_text(url)


def test_every_indexed_fixture_file_exists() -> None:
    directory = ROOT / "fixtures" / "http"
    raw: object = json.loads((directory / "index.json").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    index = cast("dict[str, str]", raw)
    for key, name in index.items():
        if key.startswith("_"):
            continue
        assert (directory / name).is_file(), f"{key} names a missing fixture {name}"


def test_demo_discovery_runs_offline_and_screens_the_fixture_boards(demo_env: Path) -> None:
    conn = connect(demo_env / "demo.db")
    try:
        summary = run_discovery(conn, DiscoveryOptions(notify=False))
    finally:
        conn.close()
    # Apify is the one paid, non-fixtured source and must not be attempted.
    assert "apify_linkedin" not in cast("list[str]", summary["sources_run"])
    # The README quotes these counts as what a stranger sees. Changing the
    # fixtures is allowed; leaving the README describing the old ones is not.
    assert summary["fetched_count"] == 15
    assert summary["new_prospects"] == 8
    # The demo has to show the policy working, not only the happy path.
    assert summary["rejected_counts"] == {"title": 6, "location says hybrid/on-site": 1}


def test_demo_discovery_needs_no_environment_keys(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    for key in ("APIFY_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    conn = connect(demo_env / "demo.db")
    try:
        with caplog.at_level("WARNING"):
            # notify stays on: the run must reach the notify branch and decline
            # it, which is where the missing-token warning came from.
            summary = run_discovery(conn, DiscoveryOptions(notify=True))
    finally:
        conn.close()
    for entry in cast("list[object]", summary["source_summaries"]):
        assert isinstance(entry, dict)
        assert not entry.get("errors"), entry
        assert not entry.get("board_errors"), entry
    # A stranger watching the run log must see nothing that looks broken.
    assert "TELEGRAM" not in caplog.text
    assert not caplog.text.strip(), caplog.text


def test_profile_seeds_all_name_a_committed_example(tmp_path: Path) -> None:
    conn = connect(tmp_path / "seed.db")
    try:
        assert seed_profile_documents(conn) == []
        stored = conn.execute("SELECT kind, name FROM profile_documents").fetchall()
    finally:
        conn.close()
    assert len(stored) == len(PROFILE_SEEDS)


def test_seed_demo_db_fills_jobs_and_profile_documents(demo_env: Path) -> None:
    db_path = seed_demo_db()
    conn = sqlite3.connect(db_path)
    try:
        jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        documents = conn.execute("SELECT COUNT(*) FROM profile_documents").fetchone()[0]
    finally:
        conn.close()
    assert jobs > 0
    assert documents == len(PROFILE_SEEDS)


def test_reseeding_drops_discovery_state_so_a_second_run_finds_things(
    demo_env: Path,
) -> None:
    """Wiping the tracker without wiping the seen-state left the demo in the
    worst possible shape: a successful discovery run that adds nothing,
    because every posting was recorded as seen by the previous boot."""
    seed_demo_db()
    conn = connect(demo_db_path())
    try:
        first = run_discovery(conn, DiscoveryOptions(notify=False))
    finally:
        conn.close()
    assert first["new_prospects"] == 8

    seed_demo_db()
    conn = connect(demo_db_path())
    try:
        second = run_discovery(conn, DiscoveryOptions(notify=False))
    finally:
        conn.close()
    assert second["new_prospects"] == 8


def test_api_serves_the_spa_and_still_answers_under_the_api_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>harrier</title>", encoding="utf-8")
    client = TestClient(create_app(spa_dir=dist))
    # The built SPA calls /api/... with no Vite proxy in front of it.
    assert client.get("/api/health").json()["name"] == "harrier"
    assert client.get("/health").json()["name"] == "harrier"
    assert "<title>harrier</title>" in client.get("/").text


def test_api_without_a_built_spa_still_serves_the_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    client = TestClient(create_app(spa_dir=tmp_path / "absent"))
    assert client.get("/api/health").status_code == 200


def test_http_fixtures_are_off_unless_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    monkeypatch.delenv("HARRIER_HTTP_FIXTURES", raising=False)
    assert http_fixtures_dir() is None


# --- the mechanical privacy pass (docs/privacy-plan.md) ----------------------

# Hosts a fixture may legitimately name: the ATS API endpoints the importers
# address by construction. Every other host must be a reserved example name.
ATS_HOSTS = frozenset(
    {
        "boards-api.greenhouse.io",
        "boards.greenhouse.io",
        "api.ashbyhq.com",
        "jobs.ashbyhq.com",
        "api.lever.co",
        "api.eu.lever.co",
        "jobs.lever.co",
        "jobs.eu.lever.co",
        "remoteok.com",
        "linkedin.com",
        "www.linkedin.com",
    }
)
RESERVED_SUFFIXES = (".example", ".test", ".invalid", ".localhost")
RESERVED_DOMAINS = ("example.com", "example.net", "example.org")
URL_RE = re.compile(r"https?://[^\s\"'<>)\\]+")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def reviewed_files() -> list[Path]:
    files = sorted((ROOT / "fixtures").rglob("*"))
    files += sorted((ROOT / "config").glob("*.example.*"))
    return [path for path in files if path.is_file()]


def is_reserved(host: str) -> bool:
    return host.endswith(RESERVED_SUFFIXES) or any(
        host == domain or host.endswith(f".{domain}") for domain in RESERVED_DOMAINS
    )


def test_reviewed_file_set_is_not_empty() -> None:
    # A refactor that moves fixtures must not silently empty this pass.
    assert len(reviewed_files()) >= 8


def test_fixtures_name_only_reserved_or_ats_hosts() -> None:
    offenders: list[str] = []
    for path in reviewed_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in cast("list[str]", URL_RE.findall(text)):
            host = (urlparse(match).hostname or "").lower()
            if host and not is_reserved(host) and host not in ATS_HOSTS:
                offenders.append(f"{path.relative_to(ROOT)}: {match}")
    assert not offenders, "non-reserved host in a public fixture: " + "; ".join(offenders)


def test_real_ats_hosts_carry_only_example_board_names() -> None:
    """A reserved host cannot leak a real employer, but boards.greenhouse.io
    can: the company rides in the path. Rather than maintain a list of which
    segments are structural, require the example name somewhere in the URL,
    exempting only a bare API root that names no company at all."""
    boards = ATS_HOSTS - {"linkedin.com", "www.linkedin.com"}
    offenders: list[str] = []
    for path in reviewed_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in cast("list[str]", URL_RE.findall(text)):
            parsed = urlparse(match)
            if (parsed.hostname or "").lower() not in boards:
                continue
            segments = [part for part in parsed.path.split("/") if part]
            if segments == ["api"] or "example" in match.lower():
                continue
            offenders.append(f"{path.relative_to(ROOT)}: {match}")
    assert not offenders, "real-looking board name in a public fixture: " + "; ".join(offenders)


def test_fixtures_contain_no_address_outside_the_example_domains() -> None:
    offenders: list[str] = []
    for path in reviewed_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for address in cast("list[str]", EMAIL_RE.findall(text)):
            domain = address.rsplit("@", 1)[1].lower()
            if not is_reserved(domain):
                offenders.append(f"{path.relative_to(ROOT)}: {address}")
    assert not offenders, "non-example address in a public fixture: " + "; ".join(offenders)
