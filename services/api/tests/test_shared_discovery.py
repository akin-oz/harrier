"""Discovery places each posting by the ownership it demands (spec 139).

Every database is built under `tmp_path`, the configuration is the committed
examples copied under their real names, and every company and posting is
synthetic (spec 060, ADR-008).
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

import harrier.discovery as discovery_module
from harrier.connection import Connection
from harrier.db import connect
from harrier.discovery import DiscoveryOptions, run_discovery
from harrier.screening.config import load_candidate_config
from harrier.screening.normalized import NormalizedJob, make_normalized_job
from harrier.screening.policy import policy_version
from harrier.screening.seen import REJECTED, SeenDecision, load_seen, save_seen
from harrier.tracker import list_jobs
from harrier.tracker.store import OtherVerdict, TrackerError, add_job, other_verdicts
from harrier.tracks import default_scope, resolve_scope
from harrier_cli.main import main

FOCUSED = "bounded"
CONFIG = Path(__file__).resolve().parents[3] / "config"
EXAMPLE = CONFIG / "screening.focused.example.json"
REMOTE_EMEA = "Remote, Europe"
FIGMA = "Build interfaces from Figma designs with React and TypeScript."


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data directory, and the committed example configs under their real
    names in an isolated working directory (as tests/test_discovery.py)."""
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    workspace = tmp_path / "workspace"
    (workspace / "config").mkdir(parents=True)
    for example in CONFIG.glob("*.example.*"):
        real = example.name.replace(".example.", ".", 1)
        (workspace / "config" / real).write_bytes(example.read_bytes())
    monkeypatch.chdir(workspace)
    return tmp_path


@pytest.fixture()
def shared(env: Path, capsys: pytest.CaptureFixture[str]) -> Iterator[Path]:
    """A configured focused track beside the industry track."""
    assert main(["tracks", "add", FOCUSED, "--kind", "focused", "--label", "Bounded"]) == 0
    assert main(["--track", FOCUSED, "profile", "put", "screening", "--file", str(EXAMPLE)]) == 0
    capsys.readouterr()
    yield env


def posting(company: str, title: str, description: str = FIGMA) -> NormalizedJob:
    slug = company.lower().replace(" ", "-")
    return make_normalized_job(
        source="remoteok",
        company=company,
        title=title,
        location=REMOTE_EMEA,
        url=f"https://jobs.example.com/{slug}",
        description=description,
        external_id=slug,
    )


def discover(monkeypatch: pytest.MonkeyPatch, jobs: list[NormalizedJob]) -> None:
    monkeypatch.setattr(discovery_module, "fetch_remoteok_jobs", lambda: list(jobs))
    conn = connect()
    try:
        run_discovery(
            conn,
            default_scope(conn),
            DiscoveryOptions(dry_run=False, notify=False, only_sources=frozenset({"remoteok"})),
        )
    finally:
        conn.close()


def rows(slug: str | None) -> dict[str, dict[str, str]]:
    conn = connect()
    try:
        scope = default_scope(conn) if slug is None else resolve_scope(conn, slug)
        return {job["company"]: job for job in list_jobs(conn, scope)}
    finally:
        conn.close()


def seen() -> dict[str, SeenDecision]:
    return load_seen("remoteok")


def verdict_count() -> int:
    conn = connect()
    try:
        row = conn.execute("SELECT count(*) FROM job_verdicts").fetchone()
        assert row is not None
        return int(row[0])
    finally:
        conn.close()


def printed(capsys: pytest.CaptureFixture[str], slug: str | None, verb: str = "next") -> str:
    capsys.readouterr()
    argv = [] if slug is None else ["--track", slug]
    assert main([*argv, verb]) == 0
    return capsys.readouterr().out


# --- without a focused track -------------------------------------------------


def test_discovery_without_a_focused_track_is_unchanged(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No focused track: no demand signal, no verdict, and the seen state
    records the industry policy version, as before spec 139."""
    discover(monkeypatch, [posting("Example Studio", "Junior Frontend Developer")])
    row = rows(None)["Example Studio"]
    assert not any(signal.startswith("demand=") for signal in row["signals"].split("|"))
    assert verdict_count() == 0
    conn: Connection = connect()
    try:
        industry = policy_version(load_candidate_config(conn))
    finally:
        conn.close()
    assert {entry.policy for entry in seen().values()} == {industry}


def test_an_archived_focused_track_is_no_focused_track(
    shared: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["tracks", "archive", FOCUSED]) == 0
    discover(monkeypatch, [posting("Example Studio", "Junior Frontend Developer")])
    assert "Example Studio" in rows(None)
    assert verdict_count() == 0


# --- placement ---------------------------------------------------------------


def test_a_bounded_posting_goes_to_the_focused_track(
    shared: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    discover(monkeypatch, [posting("Example Studio", "Junior Frontend Developer")])
    row = rows(FOCUSED)["Example Studio"]
    signals = row["signals"].split("|")
    assert signals[0] == "scorer=focused"
    assert "demand=bounded" in signals
    assert "Example Studio" not in rows(None)
    out = printed(capsys, FOCUSED, "review")
    assert "Example Studio" in out
    assert "also job: " in out
    assert "also job: rejected" not in out


def test_a_broad_posting_stays_in_industry(
    shared: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    discover(monkeypatch, [posting("Big Co", "Staff Frontend Engineer")])
    row = rows(None)["Big Co"]
    assert "demand=broad" in row["signals"].split("|")
    out = printed(capsys, None)
    assert f"also {FOCUSED}: " in out


def test_a_senior_title_alone_is_not_broad(shared: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    discover(monkeypatch, [posting("Senior Co", "Senior Frontend Engineer")])
    assert "demand=bounded" in rows(FOCUSED)["Senior Co"]["signals"].split("|")


def test_a_description_signal_makes_demand_broad(
    shared: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owning = f"{FIGMA} You will take end-to-end ownership of the product."
    discover(monkeypatch, [posting("Owner Co", "Junior Frontend Developer", owning)])
    assert "demand=broad" in rows(None)["Owner Co"]["signals"].split("|")


def test_a_posting_only_the_focused_track_passes(
    shared: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The industry title rule rejects "UI Developer"; the focused one does
    not. The row is the focused track's and names the industry rejection."""
    discover(monkeypatch, [posting("Pixel Co", "UI Developer")])
    assert "Pixel Co" in rows(FOCUSED)
    out = printed(capsys, FOCUSED)
    assert "also job: rejected (title)" in out


def test_a_posting_both_reject_has_no_row(shared: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    discover(monkeypatch, [posting("Numbers Co", "Data Engineer")])
    assert "Numbers Co" not in rows(None)
    assert "Numbers Co" not in rows(FOCUSED)
    assert [entry.reason for entry in seen().values()] == ["title"]
    assert verdict_count() == 0


def test_discovery_without_focused_configuration_falls_back_to_industry(
    env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert main(["tracks", "add", FOCUSED, "--kind", "focused", "--label", "Bounded"]) == 0
    with caplog.at_level(logging.WARNING, logger="harrier.discovery"):
        discover(
            monkeypatch,
            [
                posting("Example Studio", "Junior Frontend Developer"),
                posting("Big Co", "Staff Frontend Engineer"),
            ],
        )
    assert set(rows(None)) == {"Example Studio", "Big Co"}
    assert rows(FOCUSED) == {}
    warnings = [
        r.getMessage() for r in caplog.records if "no screening configuration" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert f"harrier --track {FOCUSED} profile put screening --file PATH" in warnings[0]


def test_a_moved_row_shows_no_verdict_for_its_own_track(
    shared: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A row moved into the track its verdict names keeps the verdict, and
    the line shows none: it is now in that track (specs 138, 139)."""
    discover(monkeypatch, [posting("Example Studio", "Junior Frontend Developer")])
    job_id = rows(FOCUSED)["Example Studio"]["id"]
    assert main(["--track", FOCUSED, "move", job_id, "--to", "job"]) == 0
    assert verdict_count() == 1
    assert "also " not in printed(capsys, None)


# --- the store -----------------------------------------------------------------


def test_a_verdict_for_the_rows_own_track_is_refused(env: Path) -> None:
    conn = connect()
    try:
        scope = default_scope(conn)
        with pytest.raises(TrackerError, match="did not go to"):
            add_job(
                conn,
                {"company": "Example Studio", "title": "Frontend Developer"},
                scope=scope,
                other_verdict=OtherVerdict(track_id=scope.track.id, verdict="pass"),
            )
        assert list_jobs(conn, scope) == []
        assert other_verdicts(conn, scope) == {}
    finally:
        conn.close()


# --- reconsider on either track (spec 139, amendment 1) --------------------------


def test_a_shared_run_records_the_shared_policy(
    shared: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A change to the focused document makes a shared run's rejections
    stale, so `reconsider` can reopen them."""
    discover(monkeypatch, [posting("Numbers Co", "Data Engineer")])
    conn = connect()
    try:
        industry = policy_version(load_candidate_config(conn))
    finally:
        conn.close()
    recorded = {entry.policy for entry in seen().values()}
    assert recorded and industry not in recorded


def test_reconsider_on_a_focused_track_reads_the_shared_state(
    shared: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    discover(monkeypatch, [posting("Numbers Co", "Data Engineer")])
    assert main(["--track", FOCUSED, "reconsider", "--source", "remoteok"]) == 0
    assert "nothing is eligible to clear" in capsys.readouterr().out
    # The focused document changes: the shared rejection is now stale.
    changed = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    changed["bonuses"][0]["points"] = 9
    edited = tmp_path / "screening.json"
    edited.write_text(json.dumps(changed), encoding="utf-8")
    assert main(["--track", FOCUSED, "profile", "put", "screening", "--file", str(edited)]) == 0
    capsys.readouterr()
    assert main(["--track", FOCUSED, "reconsider", "--source", "remoteok", "--apply"]) == 0
    assert "1 cleared" in capsys.readouterr().out
    assert seen() == {}


def test_reconsider_never_reopens_a_focused_rejection(
    shared: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """A posting the operator rejected on the focused track stays closed,
    whichever track runs `reconsider`."""
    discover(monkeypatch, [posting("Example Studio", "Junior Frontend Developer")])
    job_id = rows(FOCUSED)["Example Studio"]["id"]
    assert main(["--track", FOCUSED, "reject", job_id, "not", "a", "fit"]) == 0
    # Mark the seen entry rejected under an old policy, as a rule change would
    # leave it, so only the human rejection can protect it.
    stale = {
        key: replace(entry, verdict=REJECTED, policy="old-policy") for key, entry in seen().items()
    }
    save_seen("remoteok", stale)
    capsys.readouterr()
    for argv in (["reconsider"], ["--track", FOCUSED, "reconsider"]):
        assert main([*argv, "--source", "remoteok", "--apply"]) == 0
        assert "nothing is eligible to clear" in capsys.readouterr().out
    assert len(seen()) == 1
