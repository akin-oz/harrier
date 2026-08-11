"""A screening decision can be reconsidered when the rules change (spec 031).

The defect: a posting was marked seen before any gate decided, the record
carried no verdict and no policy, and so a rejection was permanent and
anonymous. Every screening fix was retroactively worthless, which matters
because spec 032 is a set of screening fixes.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from harrier.db import connect
from harrier.screening import rules
from harrier.screening.config import load_candidate_config
from harrier.screening.policy import UNKNOWN_POLICY, policy_version
from harrier.screening.reconsider import human_rejected_keys, reconsider_source
from harrier.screening.seen import (
    ACCEPTED,
    REJECTED,
    UNKNOWN,
    SeenDecision,
    load_seen,
    load_seen_keys,
    save_seen,
)
from harrier.tracker.store import add_job, set_status


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    return tmp_path


@pytest.fixture
def cfg() -> dict[str, Any]:
    return load_candidate_config()


def decision(verdict: str = REJECTED, policy: str = "abc123", at: str = "2026-08-01T00:00:00Z"):
    return SeenDecision(verdict, "low_score", policy, at)


# --- the policy version -----------------------------------------------------


def test_the_policy_version_is_stable_for_the_same_rules(cfg: dict[str, Any]) -> None:
    """Two runs on an unchanged configuration must agree, or every run would
    look like a rule change and reconsider everything."""
    assert policy_version(cfg) == policy_version(cfg)


def test_changing_a_scoring_weight_changes_the_version(cfg: dict[str, Any]) -> None:
    before = policy_version(cfg)
    changed = dict(cfg)
    changed["scoring"] = {**cfg.get("scoring", {}), "remote_bonus": 99}
    assert policy_version(changed) != before


def test_changing_a_keyword_list_changes_the_version(cfg: dict[str, Any]) -> None:
    before = policy_version(cfg)
    changed = dict(cfg)
    changed["include_keywords"] = ["something-else"]
    assert policy_version(changed) != before


def test_changing_a_rule_table_in_code_changes_the_version(
    cfg: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tables compiled into the module decide as much as configuration
    does: spec 032 corrects two of them. Reading them at call time rather
    than binding at import is what makes this hold."""
    before = policy_version(cfg)
    monkeypatch.setattr(rules, "SCORE_CUTOFF", rules.SCORE_CUTOFF + 5)
    assert policy_version(cfg) != before


def test_the_version_is_short_enough_to_read(cfg: dict[str, Any]) -> None:
    assert len(policy_version(cfg)) == 12


# --- the record -------------------------------------------------------------


def test_a_decision_round_trips(env: Path) -> None:
    save_seen("greenhouse", {"k1": decision()})
    loaded = load_seen("greenhouse")
    assert loaded["k1"].verdict == REJECTED
    assert loaded["k1"].reason == "low_score"
    assert loaded["k1"].policy == "abc123"


def test_an_absent_state_file_reads_as_empty(env: Path) -> None:
    assert load_seen("greenhouse") == {}


def test_the_old_format_migrates_to_unknown_rather_than_being_discarded(env: Path) -> None:
    """Discarding would re-offer every posting the operator already rejected.
    Treating them as current-policy rejections would hide them from the first
    reconsideration, which is the one that matters."""
    path = Path(str(env / "data" / "discovery"))
    path.mkdir(parents=True, exist_ok=True)
    (path / "greenhouse_seen.json").write_text(
        json.dumps({"seen_keys": ["k1", "k2"], "updated_at": "2026-06-01T00:00:00Z"}),
        encoding="utf-8",
    )
    loaded = load_seen("greenhouse")
    assert set(loaded) == {"k1", "k2"}
    assert all(item.verdict == UNKNOWN for item in loaded.values())
    assert all(item.policy == UNKNOWN_POLICY for item in loaded.values())


def test_a_migrated_entry_is_never_read_as_an_acceptance(env: Path) -> None:
    """An unknown verdict must not let a posting into the tracker without a
    fresh decision."""
    path = env / "data" / "discovery"
    path.mkdir(parents=True, exist_ok=True)
    (path / "greenhouse_seen.json").write_text(json.dumps({"seen_keys": ["k1"]}), encoding="utf-8")
    assert load_seen("greenhouse")["k1"].verdict != ACCEPTED


def test_membership_still_answers_the_dedupe_question(env: Path) -> None:
    save_seen("greenhouse", {"k1": decision(), "k2": decision(ACCEPTED)})
    assert load_seen_keys("greenhouse") == {"k1", "k2"}


def test_eviction_keeps_the_newest_not_the_lexicographically_largest(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old rule sorted keys and kept the tail, which is stable, so the
    same entries were evicted on every run forever while genuinely stale ones
    were retained."""
    monkeypatch.setattr("harrier.screening.seen.SEEN_CAP", 2)
    save_seen(
        "greenhouse",
        {
            "aaa-newest": decision(at="2026-08-10T00:00:00Z"),
            "zzz-oldest": decision(at="2026-01-01T00:00:00Z"),
            "mmm-middle": decision(at="2026-06-01T00:00:00Z"),
        },
    )
    kept = set(load_seen("greenhouse"))
    assert kept == {"aaa-newest", "mmm-middle"}, "the oldest should go, not the smallest key"


# --- the pipeline records after deciding ------------------------------------


def test_a_posting_is_recorded_only_after_a_verdict_exists(env: Path, cfg: dict[str, Any]) -> None:
    """The founding defect. Adding on sight meant a posting suppressed before
    any gate ran could never be judged later."""
    from test_screening import build_job

    from harrier.screening.pipeline import TrackerIndexes, screen_jobs

    seen: dict[str, SeenDecision] = {}
    job = build_job(title="Engineering Manager")  # rejected by the title gate
    screen_jobs(
        [job],
        candidate_cfg=cfg,
        hold_companies=set(),
        indexes=TrackerIndexes(),
        source_seen=seen,
        cache_descriptions=False,
    )
    assert len(seen) == 1
    recorded = next(iter(seen.values()))
    assert recorded.verdict == REJECTED
    assert recorded.reason == "title"
    assert recorded.policy == policy_version(cfg)


def test_an_accepted_posting_records_its_acceptance(env: Path, cfg: dict[str, Any]) -> None:
    from test_screening import build_job

    from harrier.screening.pipeline import TrackerIndexes, screen_jobs

    seen: dict[str, SeenDecision] = {}
    result = screen_jobs(
        [build_job()],
        candidate_cfg=cfg,
        hold_companies=set(),
        indexes=TrackerIndexes(),
        source_seen=seen,
        cache_descriptions=False,
    )
    assert result.new_tracker_rows
    assert next(iter(seen.values())).verdict == ACCEPTED


def test_the_reason_recorded_is_the_gate_that_decided(env: Path, cfg: dict[str, Any]) -> None:
    from test_screening import build_job

    from harrier.screening.pipeline import TrackerIndexes, screen_jobs

    seen: dict[str, SeenDecision] = {}
    screen_jobs(
        [build_job(company="HeldCo")],
        candidate_cfg=cfg,
        hold_companies={"heldco"},
        indexes=TrackerIndexes(),
        source_seen=seen,
        cache_descriptions=False,
    )
    assert next(iter(seen.values())).reason == "hold"


# --- reconsideration --------------------------------------------------------


@pytest.fixture
def db(env: Path) -> sqlite3.Connection:
    return connect()


def test_a_rejection_under_an_older_policy_is_cleared(
    db: sqlite3.Connection, cfg: dict[str, Any]
) -> None:
    save_seen("greenhouse", {"k1": decision(policy="old-policy")})
    report = reconsider_source(db, "greenhouse", cfg, dry_run=False)
    assert report.changed == 1
    assert load_seen("greenhouse") == {}


def test_a_rejection_under_the_current_policy_is_left_alone(
    db: sqlite3.Connection, cfg: dict[str, Any]
) -> None:
    """Otherwise every reconsideration would re-offer everything, and the
    operator would see the same rejected postings on a loop."""
    save_seen("greenhouse", {"k1": decision(policy=policy_version(cfg))})
    report = reconsider_source(db, "greenhouse", cfg, dry_run=False)
    assert report.changed == 0
    assert report.kept == 1
    assert set(load_seen("greenhouse")) == {"k1"}


def test_an_acceptance_is_never_reconsidered(db: sqlite3.Connection, cfg: dict[str, Any]) -> None:
    """It already produced a tracker row. Re-running it would at best do
    nothing and at worst duplicate the row."""
    save_seen("greenhouse", {"k1": decision(ACCEPTED, policy="old-policy")})
    report = reconsider_source(db, "greenhouse", cfg, dry_run=False)
    assert report.changed == 0
    assert set(load_seen("greenhouse")) == {"k1"}


def test_a_migrated_entry_is_eligible_for_the_first_reconsideration(
    db: sqlite3.Connection, cfg: dict[str, Any]
) -> None:
    save_seen("greenhouse", {"k1": SeenDecision(UNKNOWN, "", UNKNOWN_POLICY, "2026-01-01")})
    assert reconsider_source(db, "greenhouse", cfg, dry_run=False).changed == 1


def test_a_dry_run_reports_without_changing_anything(
    db: sqlite3.Connection, cfg: dict[str, Any]
) -> None:
    save_seen("greenhouse", {"k1": decision(policy="old-policy")})
    report = reconsider_source(db, "greenhouse", cfg, dry_run=True)
    assert report.changed == 1
    assert set(load_seen("greenhouse")) == {"k1"}


def test_a_job_the_operator_rejected_is_never_resurrected(
    db: sqlite3.Connection, cfg: dict[str, Any]
) -> None:
    """The refusal that matters more than the feature. Rules get to change
    their own minds, not somebody else's."""
    job_id = add_job(
        db,
        {
            "company": "Northwind Labs",
            "title": "Senior Frontend Engineer",
            "url": "https://boards.example.com/northwind/1",
            "source": "greenhouse",
            "location": "Remote, Europe",
        },
    )
    set_status(db, job_id, "rejected")

    save_seen(
        "greenhouse",
        {"https://boards.example.com/northwind/1": decision(policy="old-policy")},
    )
    report = reconsider_source(db, "greenhouse", cfg, dry_run=False)
    assert report.protected == 1
    assert report.changed == 0
    assert len(load_seen("greenhouse")) == 1


def test_a_job_the_operator_has_not_rejected_is_not_protected(
    db: sqlite3.Connection, cfg: dict[str, Any]
) -> None:
    job_id = add_job(
        db,
        {
            "company": "Northwind Labs",
            "title": "Senior Frontend Engineer",
            "url": "https://boards.example.com/northwind/1",
            "source": "greenhouse",
            "location": "Remote, Europe",
        },
    )
    set_status(db, job_id, "shortlisted")
    assert human_rejected_keys(db) == set()


def test_the_protection_matches_on_company_and_title_too(
    db: sqlite3.Connection, cfg: dict[str, Any]
) -> None:
    """A reconsidered posting arriving under a slightly different URL would
    defeat a url-only check entirely."""
    job_id = add_job(
        db,
        {
            "company": "Northwind Labs",
            "title": "Senior Frontend Engineer",
            "url": "https://boards.example.com/northwind/1",
            "source": "greenhouse",
            "location": "Remote, Europe",
        },
    )
    set_status(db, job_id, "rejected")
    assert "northwind labs|senior frontend engineer" in human_rejected_keys(db)
