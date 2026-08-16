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
import subprocess
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
from harrier.mail.watch import fetch_recent_messages
from harrier.notify import send_telegram_message
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


def test_config_resolution_ignores_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Launching the demo from a directory that happens to hold a config tree
    must not put that unknown configuration in front of a stranger (review
    finding on PR #18)."""
    decoy = tmp_path / "config"
    decoy.mkdir()
    (decoy / "feeds.example.txt").write_text(
        "https://boards.greenhouse.io/decoy\n", encoding="utf-8"
    )
    monkeypatch.setenv("HARRIER_DEMO", "1")
    monkeypatch.chdir(tmp_path)
    assert parse_ats_feeds()["greenhouse"] == ["https://boards.greenhouse.io/exampleco"]


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


def test_malformed_fixture_index_raises_offline_fixture_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One exception type for every fixture configuration failure, so callers
    # never catch two (review finding on PR #18).
    (tmp_path / "index.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("HARRIER_HTTP_FIXTURES", str(tmp_path))
    with pytest.raises(OfflineFixtureError, match="could not be read as JSON"):
        request_text("https://example.test/thing")


def test_demo_never_sends_telegram_even_with_credentials_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hazard is a present credential, not a missing one: a demo run on
    the owner's machine must not message their real chat (review finding)."""
    monkeypatch.setenv("HARRIER_DEMO", "1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token-not-real")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")

    def explode(*args: object, **kwargs: object) -> object:
        raise AssertionError("demo mode must not open a connection")

    monkeypatch.setattr("harrier.notify.urllib.request.urlopen", explode)
    assert send_telegram_message("hello") == 2


def test_demo_refuses_to_read_a_real_mailbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARRIER_DEMO", "1")
    with pytest.raises(RuntimeError, match="refusing to read a real mailbox"):
        fetch_recent_messages()


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


# --- the privacy pass over the test tree and the aggregate class (spec 044) ---
#
#
# The pass above reads fixtures/ and config/*.example.* only. That scope is why
# a real employer survived in services/api/tests/: a real board slug, its
# recruiting mailbox, and the subject and body of a real acknowledgement email
# sat in three test files through every green run. Nothing scanned them.
#
# And every check here matched a named entity, which is the class that stopped
# recurring. The class that kept recurring is the aggregate: a count measured
# from the real tracker, which names nobody and describes the search exactly.
# It had no check anywhere until this one.

BOARD_HOSTS = ATS_HOSTS - {"linkedin.com", "www.linkedin.com"}

# Declared rather than inferred. A real employer entering the suite fails here
# until someone adds the slug, which makes it a reviewable act instead of a
# silent omission. Every name below is invented.
SYNTHETIC_BOARD_SLUGS = frozenset(
    {
        "a",
        "acme",
        "added-later",
        "also-gone",
        "b",
        "badco",
        "badco:",
        "c",
        "c{index}",
        "d",
        "decoy",
        "down",
        "euco",
        "example",
        "example-eu-co",
        "exampleco",
        "examplesoft",
        "from-file",
        "gone",
        "goodco",
        "hung",
        "live",
        "one",
        "only",
        "private-watchlist",
        "real-company",
        "two",
        "z",
    }
)

_API_PREFIXES = ("v1", "boards", "jobs", "posting-api", "job-board", "v0", "postings")

# Senders that are protocol facts rather than correspondents: the mail watch has
# to recognise Google's own security notices in order to ignore them.
INFRASTRUCTURE_SENDERS = frozenset({"accounts.google.com"})

# Documents that count their own rows are describing themselves, not a search.
_SELF_DESCRIBING = re.compile(r"matrix|checklist|table|document", re.IGNORECASE)

# A count paired with a tracker entity is an observation of one person's real
# search. A cap or a page size is a specification, so the qualifiers that mark
# one are exempt.
_AGGREGATE_RE = re.compile(
    r"(?<!spec )\b\d[\d,]{1,}\s+(?:rows|records|jobs|prospects|applications|contacts|"
    r"evaluation reports|reports|descriptions)\b",
    re.IGNORECASE,
)
# A duration sitting next to a failure word is a report of something that
# happened here, not a rule. Both orders are matched, because the sentence
# reads either way: "failed silently for two months" and "for two months
# nobody noticed". The window is one clause, so a duration that merely shares
# a paragraph with the word "failed" does not trip it.
_INCIDENT_WORDS = (
    r"failed|failing|fails silently|silent|silently|outage|unnoticed|undetected|"
    r"nobody noticed|no one noticed|stopped running|went unnoticed|broke"
)
_DURATION = (
    r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
    r"[\s-](?:second|minute|hour|day|week|month|year)s?"
)
_INCIDENT_DURATION_RE = re.compile(
    rf"(?:\b(?:{_INCIDENT_WORDS})\b[^.;\n]{{0,60}}?\b{_DURATION}\b"
    rf"|\b{_DURATION}\b[^.;\n]{{0,60}}?\b(?:{_INCIDENT_WORDS})\b)",
    re.IGNORECASE,
)
# `every four hours` is a cadence, which is a specification of how often
# something runs and not a measurement of how long anything was broken. The
# first version read "a job that runs every four hours and tries nothing" as
# a disclosure, which is the same false positive the aggregate check avoids
# with its own qualifier.
_CADENCE_QUALIFIER = re.compile(r"(?:every|each|per)\s*$", re.IGNORECASE)
_DURATION_RE = re.compile(rf"\b{_DURATION}\b", re.IGNORECASE)
_SPECIFICATION_QUALIFIER = re.compile(
    r"(?:capped at|cap of|up to|at most|no more than|max(?:imum)? of|per page|"
    r"page size|limit of|bounded to)\s*$",
    re.IGNORECASE,
)


def suite_files() -> list[Path]:
    return sorted((ROOT / "services" / "api" / "tests").rglob("*.py"))


# Prose lives in Markdown and in the docstrings and comments of the Python
# that carries this project's reasoning. Both are read; a tracked file of
# either kind that falls outside this set fails
# test_the_aggregate_check_reads_every_tracked_prose_file below.
#
# Generated files are read like any other. docs/parity-checklist.md is
# generated from the matrix and is exactly where an aggregate was found, so
# exempting generated output would have exempted the finding.
PROSE_SUFFIXES = (".md", ".py")

# Vendored or fixture text is not this repository's prose. Fixtures have their
# own, stricter pass above; node_modules and lockfiles are neither ours nor
# read by anyone.
PROSE_EXCLUDED_PREFIXES = ("fixtures/", "node_modules/", ".venv/")


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
    return [line for line in out.stdout.splitlines() if line]


def prose_files() -> list[Path]:
    """Every tracked prose file, derived rather than listed.

    This used to name three directories by hand, which silently excluded
    README.md, CONTRIBUTING.md, SECURITY.md and everything under apps/. An
    aggregate in any of them passed, and the non-empty-file guard could not
    tell: a file set that is merely too small still looks like a set (spec
    044, review of PR #49).
    """
    return [
        ROOT / path
        for path in tracked_files()
        if path.endswith(PROSE_SUFFIXES) and not path.startswith(PROSE_EXCLUDED_PREFIXES)
    ]


def board_slug(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if (parsed.hostname or "").lower() not in BOARD_HOSTS:
        return None
    segments = [part for part in parsed.path.split("/") if part]
    while segments and segments[0] in _API_PREFIXES:
        segments = segments[1:]
    return segments[0].lower() if segments else None


def test_the_test_tree_is_actually_scanned() -> None:
    # The scope gap this pass exists to close: an empty file set would restore it.
    assert len(suite_files()) >= 20


def test_the_test_suite_names_only_synthetic_employers() -> None:
    offenders: list[str] = []
    for path in suite_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in cast("list[str]", URL_RE.findall(text)):
            slug = board_slug(match)
            if slug is not None and slug not in SYNTHETIC_BOARD_SLUGS:
                offenders.append(f"{path.relative_to(ROOT)}: {slug}")
    assert not offenders, (
        "board slug in the test suite that is not a declared synthetic name: "
        + "; ".join(sorted(set(offenders)))
    )


def test_the_test_suite_addresses_only_reserved_domains() -> None:
    offenders: list[str] = []
    for path in suite_files():
        # Userinfo inside a URL is not an address: https://user:pass@host trips
        # the address pattern and names no correspondent.
        text = URL_RE.sub(" ", path.read_text(encoding="utf-8", errors="replace"))
        for address in cast("list[str]", EMAIL_RE.findall(text)):
            domain = address.rsplit("@", 1)[1].lower()
            if not is_reserved(domain) and domain not in INFRASTRUCTURE_SENDERS:
                offenders.append(f"{path.relative_to(ROOT)}: {address}")
    assert not offenders, "non-reserved address in the test suite: " + "; ".join(offenders)


def test_no_committed_prose_states_how_long_a_real_incident_lasted() -> None:
    """A duration attached to a real failure is operational history.

    The aggregate check below looks for counts of tracker entities and reads
    straight past "a scheduled job failed silently for two months", which is
    the same disclosure in a different unit: it measures the maintainer's own
    running system rather than stating a rule the code must satisfy. The
    review of PR #51 found one instance; a sweep found nine more, across
    specs, the governance sources, their generated copies, and two module
    docstrings.

    The failure mode is what the project needs to describe. How long it went
    unnoticed on one machine is not, and reads as a specification only until
    someone asks whose two months those were.
    """
    offenders: list[str] = []
    for path in prose_files():
        # The module that defines the rule has to be able to quote what the
        # rule forbids, in the pattern and in the sentence explaining it. The
        # aggregate check below solves the same problem with its own
        # self-describing exemption.
        if path == Path(__file__).resolve():
            continue
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            if not _INCIDENT_DURATION_RE.search(line):
                continue
            # Every duration on the line is a cadence, so nothing here says
            # how long anything was broken.
            durations = list(_DURATION_RE.finditer(line))
            if durations and all(
                _CADENCE_QUALIFIER.search(line[: found.start()].rstrip()) for found in durations
            ):
                continue
            offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()[:70]}")
    assert not offenders, "a real incident's duration is committed: " + "; ".join(offenders)


def test_no_committed_prose_states_an_aggregate_of_the_real_search() -> None:
    """A count of rows, reports, or cached descriptions is a measurement of the
    maintainer's own job search, not a rule the code must satisfy. Caps and page
    sizes are specifications and are exempt by their qualifier."""
    offenders: list[str] = []
    for path in prose_files():
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            if _SELF_DESCRIBING.search(line):
                continue
            for match in _AGGREGATE_RE.finditer(line):
                before = line[: match.start()]
                if _SPECIFICATION_QUALIFIER.search(before.rstrip()):
                    continue
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {match.group().strip()}")
    assert not offenders, "an aggregate measured from the real search is committed: " + "; ".join(
        offenders
    )


# Account names that are obviously nobody: a synthetic path in a test fixture
# is documentation, not disclosure. Declared rather than inferred, so a real
# account name fails here until someone adds it, which is a reviewable act.
PLACEHOLDER_ACCOUNTS = frozenset({"someone", "user", "youruser", "example", "me", "you"})

# Only the absolute form. A `~/` path names no account and is the portable way
# to write a home-relative location, which this repository uses 25 times for
# real things: ~/Library/LaunchAgents, ~/.local/bin/codex, ~/Backups/harrier.
#
# The review of PR #49 found a backup location committed as `~/harrier-...`,
# and the first fix here was to match tildes too. That was wrong: it flagged
# all 25 legitimate uses. What made that line a disclosure was that it named
# where the only offline copy of one person's data sits, and no path pattern
# separates that from ~/Library/LaunchAgents. It is a judgement the privacy
# reviewer makes, recorded in spec 046 rather than pretended into a regex.
_HOME_PATH_RE = re.compile(r"/(?:Users|home)/(?P<account>[a-z][a-z0-9._-]{2,})", re.IGNORECASE)


def test_no_committed_file_names_an_absolute_home_directory() -> None:
    """An absolute /Users/<name> path publishes the maintainer's account name,
    and two of them pointed a stranger at a private sibling project. The repo
    already tests this class in rendered artifacts; committed prose was not
    covered (spec 045)."""
    offenders: list[str] = []
    for path in prose_files():
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            for match in _HOME_PATH_RE.finditer(line):
                account = (match.group("account") or "").lower()
                if account and account in PLACEHOLDER_ACCOUNTS:
                    continue
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {match.group()}")
    assert not offenders, "a home path is committed: " + "; ".join(offenders)


def test_the_aggregate_check_reads_every_tracked_prose_file() -> None:
    """The non-empty-file guard cannot catch an input set that is merely too
    small, and the earlier hand-listed set was: it named three directories and
    silently excluded README.md, CONTRIBUTING.md, SECURITY.md and apps/.

    This fails when a tracked prose file falls outside the scanned set, so
    adding a document to a new directory is a visible decision rather than a
    silent exemption (spec 044, review of PR #49).
    """
    scanned = {path.relative_to(ROOT).as_posix() for path in prose_files()}
    missing = [
        path
        for path in tracked_files()
        if path.endswith(PROSE_SUFFIXES)
        and not path.startswith(PROSE_EXCLUDED_PREFIXES)
        and path not in scanned
    ]
    assert not missing, "tracked prose outside the aggregate check: " + "; ".join(missing)


def test_the_prose_set_reaches_the_files_the_old_one_missed() -> None:
    """Named individually, because these four are the ones that were exempt
    and a regression would be silent again."""
    scanned = {path.relative_to(ROOT).as_posix() for path in prose_files()}
    for path in ("README.md", "CONTRIBUTING.md", "SECURITY.md"):
        assert path in scanned, f"{path} is not scanned for aggregates"
    assert any(path.startswith("apps/") for path in scanned), "apps/ is not scanned"
