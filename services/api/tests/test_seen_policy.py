"""A screening decision can be reconsidered when the rules change (spec 031).

The defect: a posting was marked seen before any gate decided, the record
carried no verdict and no policy, and so a rejection was permanent and
anonymous. Every screening fix was retroactively worthless, which matters
because spec 032 is a set of screening fixes.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from harrier.atomicio import DamagedStateError
from harrier.connection import Connection
from harrier.db import connect
from harrier.screening import rules
from harrier.screening.config import load_candidate_config
from harrier.screening.normalized import NormalizedJob, make_normalized_job, normalize
from harrier.screening.pipeline import build_academic_row, build_tracker_row
from harrier.screening.policy import DECIDING_PATHS, UNKNOWN_POLICY, policy_version
from harrier.screening.reconsider import human_rejected_keys, reconsider_source
from harrier.screening.seen import (
    ACCEPTED,
    REJECTED,
    UNKNOWN,
    SeenDecision,
    load_seen,
    load_seen_keys,
    posting_identities,
    save_seen,
)
from harrier.tracker.store import add_job, set_status
from harrier.tracks import default_scope


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


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ("targets", "titles"),
        ("targets", "title_keywords_include"),
        ("targets", "title_keywords_exclude"),
        ("candidate", "preferred_regions"),
        ("candidate", "preferred_countries"),
        ("preferences", "domains_preferred"),
        ("preferences", "domains_secondary"),
    ],
)
def test_changing_any_deciding_key_changes_the_version(
    cfg: dict[str, Any], section: str, key: str
) -> None:
    """Against the real nested shape, one case per path.

    The first version of this test set a flat top-level key that no real
    configuration has, so it passed while the fingerprint captured nothing at
    all: changing the title keywords or the preferred countries moved no
    version and freed no stale rejection (review finding on PR #33).
    """
    before = policy_version(cfg)
    changed = deepcopy(cfg)
    changed[section][key] = ["something-the-config-never-said"]
    assert policy_version(changed) != before


def test_every_deciding_path_exists_in_the_real_configuration(cfg: dict[str, Any]) -> None:
    """The guard that would have caught it.

    A list of key names is a guess until something checks it against the
    configuration those names are supposed to describe. Renaming a key in the
    example config now fails here rather than silently emptying the digest.
    """
    missing: list[str] = []
    for section, keys in DECIDING_PATHS.items():
        values = cfg.get(section)
        assert isinstance(values, dict), f"{section} is not a section in the real configuration"
        missing.extend(f"{section}.{key}" for key in keys if key not in values)
    assert not missing, f"deciding paths absent from the real configuration: {missing}"


def test_the_domain_bonus_is_part_of_the_version(cfg: dict[str, Any]) -> None:
    """score_job reads it directly rather than through scoring_config, so it
    needs naming separately or a change to it moves no version."""
    before = policy_version(cfg)
    changed = deepcopy(cfg)
    changed["scoring"]["domain_bonus"] = {"primary": 99, "secondary": 98}
    assert policy_version(changed) != before


def test_changing_the_domain_keyword_table_changes_the_version(
    cfg: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """That table decides a bonus that moves a job across the cutoff."""
    before = policy_version(cfg)
    monkeypatch.setattr(
        rules, "DOMAIN_KEYWORDS", {**rules.DOMAIN_KEYWORDS, "invented": ["something"]}
    )
    assert policy_version(cfg) != before


def test_changing_a_rule_table_in_code_changes_the_version(
    cfg: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tables compiled into the module decide as much as configuration
    does: spec 032 corrects two of them. Reading them at call time rather
    than binding at import is what makes this hold."""
    before = policy_version(cfg)
    # This used to move SCORE_CUTOFF, which no longer exists (spec 033). The
    # scoring numbers took its place in the fingerprint, and they are the
    # better subject: the cutoff decided nothing, while a bonus decides every
    # ranking.
    monkeypatch.setattr(rules, "DEFAULT_SCORING", {**rules.DEFAULT_SCORING, "remote_bonus": 99})
    assert policy_version(cfg) != before


@pytest.mark.parametrize(
    "table",
    [
        "US_SCOPE_PATTERNS",
        "EMPLOYMENT_BLOCKER_PATTERNS",
        "AMBIGUOUS_REGION_PATTERNS",
        "US_PAYROLL_PATTERNS",
        "US_PAYROLL_REACH_PATTERNS",
    ],
)
def test_the_blocker_tables_move_the_policy_version(
    cfg: dict[str, Any], monkeypatch: pytest.MonkeyPatch, table: str
) -> None:
    """A blocker sends a posting below every eligible one, so a phrase added
    or removed reranks history exactly as a weight change does (spec 078)."""
    before = policy_version(cfg)
    current: tuple[str, ...] | frozenset[str] = getattr(rules, table)
    extended = (
        current | {r"\binvented\b"}
        if isinstance(current, frozenset)
        else (*current, r"\binvented\b")
    )
    monkeypatch.setattr(rules, table, extended)
    assert policy_version(cfg) != before


def test_policy_version_changes_when_the_model_file_changes(cfg: dict[str, Any]) -> None:
    """The learned score is part of what a decision depends on (spec 077):
    activating, retraining or removing a model moves the version."""
    from harrier.atomicio import write_bytes_atomic
    from harrier.scoring.features import FEATURE_ORDER
    from harrier.scoring.model import NO_MODEL, active_model_path, dump_model, model_document

    def model_bytes(weight: float) -> bytes:
        return dump_model(
            model_document(
                coefficients=[weight] * len(FEATURE_ORDER),
                intercept=0.0,
                p92={"skill_signal": 1.0, "preferred_signal": 1.0, "years_gap": 1.0},
                created_at="2026-10-06",
                training={},
                evaluation={},
            )
        )

    without = policy_version(cfg)
    assert without == policy_version(cfg, model=NO_MODEL)
    write_bytes_atomic(active_model_path(), model_bytes(0.5))
    first = policy_version(cfg)
    write_bytes_atomic(active_model_path(), model_bytes(-0.5))
    retrained = policy_version(cfg)
    assert len({without, first, retrained}) == 3
    active_model_path().unlink()
    assert policy_version(cfg) == without


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
def db(env: Path) -> Connection:
    return connect()


def test_a_rejection_under_an_older_policy_is_cleared(db: Connection, cfg: dict[str, Any]) -> None:
    save_seen("greenhouse", {"k1": decision(policy="old-policy")})
    report = reconsider_source(db, default_scope(db), "greenhouse", cfg, dry_run=False)
    assert report.changed == 1
    assert load_seen("greenhouse") == {}


def test_a_rejection_under_the_current_policy_is_left_alone(
    db: Connection, cfg: dict[str, Any]
) -> None:
    """Otherwise every reconsideration would re-offer everything, and the
    operator would see the same rejected postings on a loop."""
    save_seen("greenhouse", {"k1": decision(policy=policy_version(cfg))})
    report = reconsider_source(db, default_scope(db), "greenhouse", cfg, dry_run=False)
    assert report.changed == 0
    assert report.kept == 1
    assert set(load_seen("greenhouse")) == {"k1"}


def test_an_acceptance_is_never_reconsidered(db: Connection, cfg: dict[str, Any]) -> None:
    """It already produced a tracker row. Re-running it would at best do
    nothing and at worst duplicate the row."""
    save_seen("greenhouse", {"k1": decision(ACCEPTED, policy="old-policy")})
    report = reconsider_source(db, default_scope(db), "greenhouse", cfg, dry_run=False)
    assert report.changed == 0
    assert set(load_seen("greenhouse")) == {"k1"}


def test_a_migrated_entry_is_eligible_for_the_first_reconsideration(
    db: Connection, cfg: dict[str, Any]
) -> None:
    save_seen("greenhouse", {"k1": SeenDecision(UNKNOWN, "", UNKNOWN_POLICY, "2026-01-01")})
    assert reconsider_source(db, default_scope(db), "greenhouse", cfg, dry_run=False).changed == 1


def test_a_dry_run_reports_without_changing_anything(db: Connection, cfg: dict[str, Any]) -> None:
    save_seen("greenhouse", {"k1": decision(policy="old-policy")})
    report = reconsider_source(db, default_scope(db), "greenhouse", cfg, dry_run=True)
    assert report.changed == 1
    assert set(load_seen("greenhouse")) == {"k1"}


def test_a_job_the_operator_rejected_is_never_resurrected(
    db: Connection, cfg: dict[str, Any]
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
        scope=default_scope(db),
    )
    set_status(db, default_scope(db), job_id, "rejected")

    save_seen(
        "greenhouse",
        {"https://boards.example.com/northwind/1": decision(policy="old-policy")},
    )
    report = reconsider_source(db, default_scope(db), "greenhouse", cfg, dry_run=False)
    assert report.protected == 1
    assert report.changed == 0
    assert len(load_seen("greenhouse")) == 1


def _screened_posting(
    external_id: str, url: str = "https://boards.example.com/Northwind/Job-1"
) -> NormalizedJob:
    """A posting as a source delivers it. The board token differs from the
    company name, as it does on Greenhouse, so the key cannot be rebuilt from
    the company alone. The url keeps its case because the key hashes it as
    given, so a lowercased url would rebuild a different key."""
    return make_normalized_job(
        source="greenhouse",
        company="Northwind Labs",
        title="Senior Frontend Engineer",
        location="Remote, Europe",
        url=url,
        external_id=external_id,
        board_key="northwind",
    )


@pytest.mark.parametrize(
    ("external_id", "url"),
    [
        ("4001", "https://boards.example.com/Northwind/Job-1"),
        ("", "https://boards.example.com/Northwind/Job-1"),
        ("", ""),
    ],
    ids=["external-id", "url-only", "no-id-no-url"],
)
def test_a_rejected_row_is_protected_under_the_key_discovery_records(
    db: Connection, cfg: dict[str, Any], external_id: str, url: str
) -> None:
    """Discovery records a hashed `job_key`, not a url. A protection that
    compared the key against urls and company plus title never matched, so a
    stale entry for a row the operator rejected was cleared and counted as
    eligible, and the report said nobody's decision had been left alone."""
    job = _screened_posting(external_id, url)
    job_id = add_job(db, build_tracker_row(job, 80, []), scope=default_scope(db))
    set_status(db, default_scope(db), job_id, "rejected")
    save_seen("greenhouse", {job["job_key"]: decision(policy="old-policy")})

    report = reconsider_source(db, default_scope(db), "greenhouse", cfg, dry_run=False)

    assert report.protected == 1
    assert report.changed == 0
    assert set(load_seen("greenhouse")) == {job["job_key"]}


@pytest.mark.parametrize("company", ["Smith; Jones LLP", "Smith ;Jones LLP"])
def test_a_board_cut_short_in_notes_is_still_matched(
    db: Connection, cfg: dict[str, Any], company: str
) -> None:
    """LinkedIn takes the board from the company name, and notes end a value
    at `;` and drop the space before it, so the stored label reads
    `apify_linkedin:smith`. The company column is whole, so the key is still
    rebuilt."""
    job = make_normalized_job(
        source="apify_linkedin",
        company=company,
        title="Senior Frontend Engineer",
        location="Remote, Europe",
        url="https://www.linkedin.com/jobs/view/4001",
        external_id="4001",
        board_key=company,
    )
    job_id = add_job(db, build_tracker_row(job, 80, []), scope=default_scope(db))
    set_status(db, default_scope(db), job_id, "rejected")
    save_seen("apify_linkedin", {job["job_key"]: decision(policy="old-policy")})

    report = reconsider_source(db, default_scope(db), "apify_linkedin", cfg, dry_run=False)

    assert report.protected == 1
    assert report.changed == 0


@pytest.mark.parametrize("organisation", ["Smith; Jones University", "A|B University", "A=B Univ"])
def test_a_board_rewritten_by_the_academic_row_is_still_matched(
    db: Connection, cfg: dict[str, Any], organisation: str
) -> None:
    """The academic row writes its label with separators turned to spaces
    rather than cut short, so `Smith; Jones University` is stored as
    `smith  jones university`. The key was hashed from the whole
    organisation, which the company column keeps."""
    job = make_normalized_job(
        source="apify_academic",
        company=organisation,
        title="Postdoctoral Researcher",
        location="Amsterdam, Netherlands",
        url="https://positions.example.org/Job-7",
        external_id="7",
        board_key=normalize(organisation),
    )
    row = build_academic_row(job, area_hit=None, position_hit=None, flags=[])
    job_id = add_job(db, row, scope=default_scope(db))
    set_status(db, default_scope(db), job_id, "rejected")
    save_seen("apify_academic", {job["job_key"]: decision(policy="old-policy")})

    report = reconsider_source(
        db, default_scope(db), "apify_academic", None, dry_run=False, policy="current"
    )

    assert report.protected == 1
    assert report.changed == 0


def test_an_intact_board_does_not_protect_a_posting_on_another_board(
    db: Connection, cfg: dict[str, Any]
) -> None:
    """The company stands in for the board only when the label was cut
    short. A rejection on board `northwind` says nothing about job 4001 on a
    board named after the company, which is a different posting (review
    finding on PR #238)."""
    rejected = _screened_posting("4001")
    job_id = add_job(db, build_tracker_row(rejected, 80, []), scope=default_scope(db))
    set_status(db, default_scope(db), job_id, "rejected")
    other = make_normalized_job(
        source="greenhouse",
        company="Northwind Labs",
        title="Staff Platform Engineer",
        location="Remote, Europe",
        url="https://boards.example.com/northwind-labs/4001",
        external_id="4001",
        board_key="Northwind Labs",
    )
    save_seen("greenhouse", {other["job_key"]: decision(policy="old-policy")})

    report = reconsider_source(db, default_scope(db), "greenhouse", cfg, dry_run=False)

    assert report.protected == 0
    assert report.cleared == [other["job_key"]]


def test_a_row_without_a_label_is_matched_by_its_company(
    db: Connection, cfg: dict[str, Any]
) -> None:
    """A source with no board token keys by the company. A row that carries
    no label still protects that key."""
    job = make_normalized_job(
        source="greenhouse",
        company="Northwind Labs",
        title="Senior Frontend Engineer",
        location="Remote, Europe",
        url="https://boards.example.com/Northwind/Job-1",
        external_id="4001",
    )
    row = build_tracker_row(job, 80, [])
    row["notes"] = "; ".join(
        part for part in row["notes"].split("; ") if not part.startswith("source_label=")
    )
    job_id = add_job(db, row, scope=default_scope(db))
    set_status(db, default_scope(db), job_id, "rejected")
    save_seen("greenhouse", {job["job_key"]: decision(policy="old-policy")})

    report = reconsider_source(db, default_scope(db), "greenhouse", cfg, dry_run=False)

    assert report.protected == 1
    assert report.changed == 0


def test_a_row_the_operator_has_not_rejected_leaves_its_key_eligible(
    db: Connection, cfg: dict[str, Any]
) -> None:
    """The protection follows the operator's decision, not the row's
    existence: a shortlisted row's stale entry is still cleared."""
    job = _screened_posting("4001")
    job_id = add_job(db, build_tracker_row(job, 80, []), scope=default_scope(db))
    set_status(db, default_scope(db), job_id, "shortlisted")
    save_seen("greenhouse", {job["job_key"]: decision(policy="old-policy")})

    report = reconsider_source(db, default_scope(db), "greenhouse", cfg, dry_run=False)

    assert report.protected == 0
    assert report.cleared == [job["job_key"]]


# --- cross-source protection (amendment of 2026-10-11) ----------------------


def _listed_elsewhere(url: str, company: str, title: str) -> NormalizedJob:
    """The same role as `_screened_posting`, seen on LinkedIn: a different
    source, board and key, so the rejected row cannot rebuild it."""
    return make_normalized_job(
        source="apify_linkedin",
        company=company,
        title=title,
        location="Remote, Europe",
        url=url,
        external_id="9001",
        board_key=company,
    )


def _stale_with_identities(job: NormalizedJob) -> SeenDecision:
    return SeenDecision(
        REJECTED,
        "tracker_duplicate",
        "old-policy",
        "2026-08-01T00:00:00Z",
        identities=posting_identities(job["url"], job["company"], job["title"]),
    )


def _reject_on_greenhouse(db: Connection, status: str = "rejected") -> NormalizedJob:
    job = _screened_posting("4001")
    job_id = add_job(db, build_tracker_row(job, 80, []), scope=default_scope(db))
    set_status(db, default_scope(db), job_id, status)
    return job


@pytest.mark.parametrize("url", ["https://boards.example.com/Northwind/Job-1", ""])
def test_a_rejection_records_the_posting_identities(
    env: Path, cfg: dict[str, Any], url: str
) -> None:
    """Recorded at decision time, from the posting, as hashes: the url and
    the company with the title. A posting without a url has only the second."""
    from test_screening import build_job

    from harrier.screening.pipeline import TrackerIndexes, screen_jobs

    seen: dict[str, SeenDecision] = {}
    job = build_job(title="Engineering Manager", url=url)  # rejected by the title gate
    screen_jobs(
        [job],
        candidate_cfg=cfg,
        hold_companies=set(),
        indexes=TrackerIndexes(),
        source_seen=seen,
        cache_descriptions=False,
    )
    recorded = seen[job["job_key"]]
    assert recorded.verdict == REJECTED
    assert recorded.identities == posting_identities(url, job["company"], job["title"])
    assert len(recorded.identities) == (2 if url else 1)


def test_a_posting_rejected_on_another_source_is_protected(
    db: Connection, cfg: dict[str, Any]
) -> None:
    """Rejected on Greenhouse, seen on LinkedIn under its own key. The url
    is the same; the company and title are written differently, so only the
    url can match."""
    rejected = _reject_on_greenhouse(db)
    elsewhere = _listed_elsewhere(rejected["url"], "Northwind Labs Ltd", "Senior FE Engineer")
    save_seen("apify_linkedin", {elsewhere["job_key"]: _stale_with_identities(elsewhere)})

    report = reconsider_source(db, default_scope(db), "apify_linkedin", cfg, dry_run=False)

    assert report.protected == 1
    assert report.changed == 0
    assert set(load_seen("apify_linkedin")) == {elsewhere["job_key"]}


def test_a_cross_source_match_on_company_and_title_is_protected(
    db: Connection, cfg: dict[str, Any]
) -> None:
    """The common cross-source case: the same role under another url."""
    rejected = _reject_on_greenhouse(db)
    elsewhere = _listed_elsewhere(
        "https://www.linkedin.com/jobs/view/9001", rejected["company"], rejected["title"]
    )
    save_seen("apify_linkedin", {elsewhere["job_key"]: _stale_with_identities(elsewhere)})

    report = reconsider_source(db, default_scope(db), "apify_linkedin", cfg, dry_run=False)

    assert report.protected == 1
    assert report.changed == 0


def test_identities_of_a_row_not_rejected_protect_nothing(
    db: Connection, cfg: dict[str, Any]
) -> None:
    rejected = _reject_on_greenhouse(db, status="shortlisted")
    elsewhere = _listed_elsewhere(rejected["url"], rejected["company"], rejected["title"])
    save_seen("apify_linkedin", {elsewhere["job_key"]: _stale_with_identities(elsewhere)})

    report = reconsider_source(db, default_scope(db), "apify_linkedin", cfg, dry_run=False)

    assert report.protected == 0
    assert report.cleared == [elsewhere["job_key"]]


def test_an_entry_without_identities_keeps_key_protection_only(
    db: Connection, cfg: dict[str, Any]
) -> None:
    """An entry recorded before identities existed. Its own source's row
    still protects it by key; another source's row cannot reach it."""
    rejected = _reject_on_greenhouse(db)
    elsewhere = _listed_elsewhere(rejected["url"], rejected["company"], rejected["title"])
    save_seen("greenhouse", {rejected["job_key"]: decision(policy="old-policy")})
    save_seen("apify_linkedin", {elsewhere["job_key"]: decision(policy="old-policy")})

    same_source = reconsider_source(db, default_scope(db), "greenhouse", cfg, dry_run=False)
    other_source = reconsider_source(db, default_scope(db), "apify_linkedin", cfg, dry_run=False)

    assert same_source.protected == 1
    assert other_source.protected == 0
    assert other_source.cleared == [elsewhere["job_key"]]


def test_identities_round_trip(env: Path) -> None:
    stored = SeenDecision(REJECTED, "title", "abc123", "2026-08-01T00:00:00Z", ("a" * 20, "b" * 20))
    save_seen("greenhouse", {"k1": stored})
    assert load_seen("greenhouse") == {"k1": stored}


def test_a_file_from_before_identities_loads_without_them(env: Path) -> None:
    path = env / "data" / "discovery" / "greenhouse_seen.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "decisions": {
                    "k1": {
                        "verdict": "rejected",
                        "reason": "title",
                        "policy": "abc123",
                        "at": "2026-08-01T00:00:00Z",
                    }
                },
                "updated_at": "2026-08-01T00:00:00Z",
            }
        )
    )
    assert load_seen("greenhouse")["k1"].identities == ()


def test_the_seen_file_holds_no_posting_text(env: Path, cfg: dict[str, Any]) -> None:
    """Hashes only: the url, company and title never reach the file, which
    spec 107 copies into hosted storage."""
    from test_screening import build_job

    from harrier.screening.pipeline import TrackerIndexes, screen_jobs

    seen: dict[str, SeenDecision] = {}
    job = build_job(title="Engineering Manager", company="Northwind Labs")
    screen_jobs(
        [job],
        candidate_cfg=cfg,
        hold_companies=set(),
        indexes=TrackerIndexes(),
        source_seen=seen,
        cache_descriptions=False,
    )
    save_seen("greenhouse", seen)
    text = (env / "data" / "discovery" / "greenhouse_seen.json").read_text().lower()

    assert seen[job["job_key"]].identities
    for value in (job["url"], job["company"], job["title"]):
        assert value.lower() not in text


def test_a_job_the_operator_has_not_rejected_is_not_protected(
    db: Connection, cfg: dict[str, Any]
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
        scope=default_scope(db),
    )
    set_status(db, default_scope(db), job_id, "shortlisted")
    assert human_rejected_keys(db, default_scope(db)) == set()


def test_the_protection_matches_on_company_and_title_too(
    db: Connection, cfg: dict[str, Any]
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
        scope=default_scope(db),
    )
    set_status(db, default_scope(db), job_id, "rejected")
    assert "northwind labs|senior frontend engineer" in human_rejected_keys(db, default_scope(db))


def test_the_recorded_reason_is_a_stable_slug(env: Path, cfg: dict[str, Any]) -> None:
    """Every gate records a slug. The remote gate used to record the prose the
    rule returns, so rewording that message would silently split one cause
    into two and a stored reason could not be grouped at all (review finding
    on PR #33)."""
    from test_screening import build_job

    from harrier.screening.pipeline import REMOTE_REGION_REASON, TrackerIndexes, screen_jobs

    seen: dict[str, SeenDecision] = {}
    screen_jobs(
        [build_job(location="Hybrid - Berlin")],
        candidate_cfg=cfg,
        hold_companies=set(),
        indexes=TrackerIndexes(),
        source_seen=seen,
        cache_descriptions=False,
    )
    reason = next(iter(seen.values())).reason
    assert reason == REMOTE_REGION_REASON
    assert " " not in reason, "a recorded reason must be groupable, not a sentence"


def test_a_corrupt_state_file_is_refused_rather_than_read_as_empty(env: Path) -> None:
    """Changed by spec 040, which is where this was promised on PR #33.

    Spec 031 logged and returned empty, which still made every previously
    rejected posting look new and still let the save that followed overwrite
    the damaged file. Raising is what actually stops that, and it is the same
    treatment the mail watch state gets, so one failure has one behaviour.
    """
    path = env / "data" / "discovery"
    path.mkdir(parents=True, exist_ok=True)
    (path / "greenhouse_seen.json").write_text("{not json at all", encoding="utf-8")
    with pytest.raises(DamagedStateError):
        load_seen("greenhouse")


def test_a_state_file_that_is_not_an_object_is_refused(env: Path) -> None:
    path = env / "data" / "discovery"
    path.mkdir(parents=True, exist_ok=True)
    (path / "greenhouse_seen.json").write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(DamagedStateError):
        load_seen("greenhouse")


def test_nothing_eligible_is_not_reported_as_everything_current(
    db: Connection, cfg: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """Zero cleared is also what a watchlist of protected manual rejections
    produces, and saying the wrong one of those is a claim about the
    operator's own decisions (review finding on PR #33)."""
    from harrier_cli.main import main

    save_seen("greenhouse", {"k1": decision(policy=policy_version(cfg))})
    assert main(["reconsider", "--source", "greenhouse"]) == 0
    out = capsys.readouterr().out
    assert "nothing is eligible to clear" in out
    assert "current rules" not in out
