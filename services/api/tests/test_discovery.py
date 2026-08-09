"""Orchestrator behavior pins (spec 011), ported from the old repo's
tests/test_run_job_imports.py plus the scheduled-policy and aggregate pins."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import harrier.discovery as discovery_module
from harrier.db import connect
from harrier.discovery import (
    DiscoveryOptions,
    apify_allowed_now,
    run_discovery,
    scheduled_apify_count,
)
from harrier.notify import build_telegram_message, send_telegram_message
from harrier.screening.normalized import NormalizedJob, make_normalized_job
from harrier.tracker import list_jobs
from harrier_cli.main import build_parser

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false


def _fake_feeds(path: Path | None = None) -> dict[str, list[str]]:
    return {
        "greenhouse": ["https://boards.greenhouse.io/exampleco"],
        "ashby": [],
        "lever": [],
    }


def _one_greenhouse_job(url: str) -> list[NormalizedJob]:
    return [_job("greenhouse", 1)]


@pytest.fixture()
def discovery_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    repo_root = Path(__file__).resolve().parents[3]
    monkeypatch.chdir(repo_root)
    return tmp_path


def _job(source: str, index: int) -> NormalizedJob:
    return make_normalized_job(
        source=source,
        company=f"Co{source}{index}",
        title="Senior Frontend Engineer",
        location="Remote, Europe",
        url=f"https://example.com/{source}/{index}",
        description=(
            "TypeScript React ownership testing ci/cd strong engineering culture remote Europe."
        ),
        external_id=f"{source}-{index}",
    )


def test_cli_parser_defaults_apify_count_to_150() -> None:
    parser = build_parser()
    args = parser.parse_args(["discover"])
    assert args.apify_count == 150
    args = parser.parse_args(["discover", "--apify-count", "350"])
    assert args.apify_count == 350


def test_apify_count_override_passes_through(
    discovery_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_apify(**kwargs: Any) -> list[NormalizedJob]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(discovery_module, "fetch_apify_linkedin_jobs", fake_apify)
    conn = connect()
    run_discovery(
        conn,
        DiscoveryOptions(
            dry_run=True,
            notify=False,
            only_sources=frozenset({"apify_linkedin"}),
            apify_count=350,
        ),
    )
    assert captured["count"] == 350


def test_scheduled_run_uses_configured_count(
    discovery_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_apify(**kwargs: Any) -> list[NormalizedJob]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(discovery_module, "fetch_apify_linkedin_jobs", fake_apify)
    conn = connect()
    run_discovery(
        conn,
        DiscoveryOptions(
            dry_run=True,
            notify=False,
            only_sources=frozenset({"apify_linkedin"}),
            scheduled=True,
            now=datetime(2026, 8, 10, 9, 0),  # a Monday morning
        ),
    )
    assert captured["count"] == scheduled_apify_count()
    assert scheduled_apify_count() == 50


def test_scheduled_policy_gates_apify() -> None:
    assert apify_allowed_now(datetime(2026, 8, 10, 9, 0)) is True  # Monday 09:00
    assert apify_allowed_now(datetime(2026, 8, 10, 13, 0)) is False  # Monday 13:00
    assert apify_allowed_now(datetime(2026, 8, 9, 9, 0)) is False  # Sunday morning


def test_scheduled_evening_run_skips_apify(
    discovery_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_apify(**kwargs: Any) -> list[NormalizedJob]:
        raise AssertionError("Apify must not run outside weekday mornings")

    monkeypatch.setattr(discovery_module, "fetch_apify_linkedin_jobs", fail_apify)
    conn = connect()
    aggregate = run_discovery(
        conn,
        DiscoveryOptions(
            dry_run=True,
            notify=False,
            only_sources=frozenset({"apify_linkedin"}),
            scheduled=True,
            now=datetime(2026, 8, 10, 20, 0),
        ),
    )
    assert aggregate["sources_run"] == []


def test_full_run_aggregates_and_notifies_once(
    discovery_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discovery_module, "parse_ats_feeds", _fake_feeds)
    monkeypatch.setattr(discovery_module, "fetch_greenhouse_jobs", _one_greenhouse_job)

    def _one_remoteok() -> list[NormalizedJob]:
        return [_job("remoteok", 1)]

    monkeypatch.setattr(discovery_module, "fetch_remoteok_jobs", _one_remoteok)
    sent: list[str] = []

    def _capture_send(message: str) -> int:
        sent.append(message)
        return 0

    monkeypatch.setattr(discovery_module, "send_telegram_message", _capture_send)

    conn = connect()
    progress_events: list[tuple[str, str]] = []
    aggregate = run_discovery(
        conn,
        DiscoveryOptions(only_sources=frozenset({"greenhouse", "remoteok"})),
        progress=lambda source, stage: progress_events.append((source, stage)),  # pyright: ignore[reportUnknownLambdaType, reportUnknownMemberType]
    )

    assert aggregate["new_prospects"] == 2
    assert aggregate["sources_run"] == ["greenhouse", "remoteok"]
    assert len(sent) == 1
    assert "2 new prospects" in sent[0]
    assert ("greenhouse", "fetching") in progress_events
    assert ("remoteok", "done") in progress_events

    # Persisted through the single write path; summaries written.
    assert len(list_jobs(conn)) == 2
    incoming = discovery_env / "data" / "incoming"
    assert (incoming / "greenhouse_latest.json").is_file()
    aggregate_file = json.loads((incoming / "job_imports_run.json").read_text(encoding="utf-8"))
    assert aggregate_file["new_prospects"] == 2


def test_dry_run_writes_nothing_and_notify_gate(
    discovery_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discovery_module, "parse_ats_feeds", _fake_feeds)
    monkeypatch.setattr(discovery_module, "fetch_greenhouse_jobs", _one_greenhouse_job)
    sent: list[str] = []

    def _capture_send(message: str) -> int:
        sent.append(message)
        return 0

    monkeypatch.setattr(discovery_module, "send_telegram_message", _capture_send)

    conn = connect()
    aggregate = run_discovery(
        conn,
        DiscoveryOptions(dry_run=True, notify=False, only_sources=frozenset({"greenhouse"})),
    )
    assert aggregate["new_prospects"] == 1
    assert list_jobs(conn) == []
    assert not (discovery_env / "data" / "incoming").exists()
    assert not (discovery_env / "data" / "discovery").exists()
    assert sent == []


def test_notify_returns_2_without_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert send_telegram_message("hello") == 2


def test_build_telegram_message_shape() -> None:
    items: list[dict[str, object]] = [
        {
            "company": "Acme",
            "title": "Senior Frontend Engineer",
            "location": "Remote, Europe",
            "fit_score": 88,
            "url": "https://example.com/1",
        }
    ]
    message = build_telegram_message(items)
    assert message.startswith("Job imports: 1 new prospects")
    assert "Acme: Senior Frontend Engineer" in message
    assert "score: 88" in message


def test_discovery_kind_is_registered() -> None:
    from harrier_api.runs import KIND_COMMANDS

    assert "discovery" in KIND_COMMANDS
    assert KIND_COMMANDS["discovery"][-1] == "discover"


def test_load_project_env_does_not_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harrier_cli.main import load_project_env

    env_file = tmp_path / ".env"
    env_file.write_text('FOO_FROM_ENV="file-value"\nEXISTING=from-file\n', encoding="utf-8")
    monkeypatch.setenv("EXISTING", "from-process")
    monkeypatch.delenv("FOO_FROM_ENV", raising=False)
    load_project_env(env_file)
    import os

    assert os.environ["FOO_FROM_ENV"] == "file-value"
    assert os.environ["EXISTING"] == "from-process"
    monkeypatch.delenv("FOO_FROM_ENV", raising=False)


def test_cli_discover_dry_run_emits_progress_protocol(
    discovery_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(discovery_module, "parse_ats_feeds", _fake_feeds)
    monkeypatch.setattr(discovery_module, "fetch_greenhouse_jobs", _one_greenhouse_job)
    from harrier_cli.main import main as cli_main

    with patch.object(discovery_module, "send_telegram_message"):
        rc = cli_main(["discover", "--dry-run", "--no-notify", "--only-source", "greenhouse"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "::harrier::" in out
    assert (
        '"message": "greenhouse: fetching"' in out.replace("'", '"')
        or "greenhouse: fetching" in out
    )
    assert '"new_prospects": 1' in out
