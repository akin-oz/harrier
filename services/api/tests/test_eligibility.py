"""Whether a posting can engage someone based in Türkiye (spec 122).

Every posting here is synthetic, with an invented company. The rule tests
call the assessor directly; the rest drive the write path, the CLI and the
API on a temporary database.

The write path's tests run on both stores through the dual-store fixture
(spec 126). A test that drives the CLI or the API, which refuse a Postgres
URL until specs 127 and 104, is marked `sqlite_only`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import DualStore, auth
from fastapi.testclient import TestClient
from pg_support import run_as_migrator
from test_screening import candidate_cfg

import harrier.discovery as discovery_module
from harrier.connection import Connection, StoreDatabaseError, StoreIntegrityError
from harrier.discovery import DiscoveryOptions, run_discovery
from harrier.scoring.features import FEATURE_ORDER
from harrier.screening import eligibility, rules
from harrier.screening.descriptions import save_description_cache
from harrier.screening.eligibility import (
    CONFIRMED,
    EXCLUDED,
    POSSIBLE,
    UNKNOWN,
    assess_eligibility,
    eligibility_version,
)
from harrier.screening.normalized import NormalizedJob, make_normalized_job
from harrier.screening.pipeline import build_tracker_indexes, screen_jobs
from harrier.screening.policy import policy_version
from harrier.tracker import schema
from harrier.tracker.actions import (
    add_manually,
    change_status,
    next_up,
    reassess_eligibility,
    set_eligibility,
)
from harrier.tracker.store import (
    EligibilityAssessment,
    EligibilityError,
    add_job,
    eligibility_history,
    eligibility_of,
    get_job,
    list_events,
    record_eligibility,
)
from harrier.tracks import Scope, add_track, default_scope, resolve_scope
from harrier_api import app as app_module
from harrier_api.app import create_app
from harrier_cli.main import main

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

ACADEMIC = "phd-search"


def posting(
    *,
    location: str = "Remote",
    description: str = "",
    title: str = "Senior Frontend Engineer",
    url: str = "https://jobs.example.com/acme/1",
    company: str = "Acme Widgets",
) -> NormalizedJob:
    return make_normalized_job(
        source="manual",
        company=company,
        title=title,
        location=location,
        url=url,
        description=description,
    )


def verdict_of(**fields: str) -> str:
    return assess_eligibility(posting(**fields)).verdict


# --- fixtures -------------------------------------------------------------------


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    return tmp_path


@pytest.fixture
def conn(env: Path, store: Connection) -> Connection:
    """The store, on both stores (spec 126). On SQLite it is the default
    path under this test's HARRIER_DATA_DIR, which the CLI and the API open."""
    return store


@pytest.fixture
def scope(conn: Connection) -> Scope:
    return default_scope(conn)


@pytest.fixture
def client(env: Path) -> TestClient:
    return TestClient(create_app())


def new_row(
    conn: Connection,
    scope: Scope,
    *,
    number: int = 1,
    location: str = "Remote, Europe",
    fit_score: str = "",
    description: str | None = None,
) -> int:
    url = f"https://jobs.example.com/acme/{number}"
    job_id = add_job(
        conn,
        {
            "company": f"Acme Widgets {number}",
            "title": "Senior Frontend Engineer",
            "location": location,
            "url": url,
            "source": "manual",
            "fit_score": fit_score,
        },
        scope=scope,
    )
    if description is not None:
        save_description_cache(url, description)
    return job_id


# --- rule verdicts ----------------------------------------------------------------


def test_a_turkiye_location_confirms() -> None:
    assessment = assess_eligibility(posting(location="Türkiye (Remote)"))
    assert assessment.verdict == CONFIRMED
    assert assessment.field == "location"
    assert assessment.excerpt == "Türkiye (Remote)"
    assert assessment.evidence_url == "https://jobs.example.com/acme/1"
    assert assessment.origin == "rule"
    assert assessment.version == eligibility_version()


def test_turkish_casing_folds() -> None:
    assert verdict_of(location="TÜRKİYE (Remote)") == CONFIRMED
    assert verdict_of(location="İstanbul, Remote") == CONFIRMED
    assert verdict_of(location="Turkey, remote") == CONFIRMED
    folded = assess_eligibility(posting(location="TÜRKİYE (Remote)"))
    # Matched folded, cut as written.
    assert folded.excerpt == "TÜRKİYE (Remote)"


def test_a_country_restriction_excludes_unless_a_route_gets_around_it() -> None:
    restricted = "A remote role. You must be based in the UK."
    excluded = assess_eligibility(posting(description=restricted))
    assert excluded.verdict == EXCLUDED
    assert excluded.excerpt == "You must be based in the UK."
    assert excluded.field == "description"

    eor = assess_eligibility(
        posting(description=f"{restricted} We hire via an employer of record.")
    )
    assert eor.verdict == POSSIBLE
    assert "eor" in eor.routes

    relocation = assess_eligibility(
        posting(description=f"{restricted} Relocation support offered.")
    )
    assert relocation.verdict == POSSIBLE
    assert "relocation" in relocation.routes

    # A route offered in the negative names no route.
    refused = assess_eligibility(posting(description=f"{restricted} No relocation support."))
    assert refused.verdict == EXCLUDED
    assert refused.routes == ()


def test_a_restriction_with_an_alternative_does_not_fire() -> None:
    assert verdict_of(description="You must be based in the UK or Türkiye.") == POSSIBLE
    assert verdict_of(description="You must be based in the UK or EU.") == UNKNOWN
    assert verdict_of(description="Right to work in Germany, or in Europe, is required.") == UNKNOWN
    # An alternative outside EMEA is no such offer.
    assert verdict_of(description="You must be based in the UK or Canada.") == EXCLUDED


def test_eu_permit_phrases_are_possible_never_excluded() -> None:
    for phrase in ("EU work permit required.", "Must be based in the EU.", "EU-based contractor."):
        assessment = assess_eligibility(posting(description=phrase))
        assert assessment.verdict == POSSIBLE, phrase
        assert "eu_permit:" in assessment.matched, phrase
    # Neither in a location, which a posting can also name its permit in.
    assert verdict_of(location="Remote (must be based in the EU)") == POSSIBLE


def test_region_words_alone_are_unknown() -> None:
    for location in ("Worldwide", "Global", "Anywhere", "EMEA", "Europe", "Remote"):
        assessment = assess_eligibility(posting(location=location))
        assert assessment.verdict == UNKNOWN, location
        assert assessment.excerpt == "", location


def test_a_contractor_route_in_region_is_possible() -> None:
    assessment = assess_eligibility(posting(location="EMEA", description="A B2B contract role."))
    assert assessment.verdict == POSSIBLE
    assert assessment.routes == ("contractor",)
    # The route without a region is recorded and decides nothing.
    alone = assess_eligibility(posting(location="Remote", description="A B2B contract role."))
    assert alone.verdict == UNKNOWN
    assert alone.routes == ("contractor",)


def test_bare_contract_is_not_a_route() -> None:
    for text in (
        "An employment contract with full benefits.",
        "A permanent contract.",
        "We build a B2B SaaS product.",
    ):
        assert "contractor" not in assess_eligibility(posting(description=text)).routes, text
    employee = assess_eligibility(posting(description="An employment contract with benefits."))
    assert employee.routes == ("employee",)


def test_us_scope_postings_are_excluded() -> None:
    # Imported from spec 078's module, not copied.
    assert eligibility.US_SCOPE_PATTERNS is rules.US_SCOPE_PATTERNS
    assert eligibility.EMPLOYMENT_BLOCKER_PATTERNS is rules.EMPLOYMENT_BLOCKER_PATTERNS
    assert eligibility.EU_PERMIT_PATTERNS is rules.EU_PERMIT_PATTERNS
    assert verdict_of(description="Open to candidates anywhere in the US.") == EXCLUDED
    assert verdict_of(description="This is a W-2 position.") == EXCLUDED
    # Spec 078's location rule: a location naming an EMEA region keeps a
    # US scope phrase from restricting, and nothing keeps W-2 from it.
    assert verdict_of(
        location="Remote, Europe", description="We also hire anywhere in the US."
    ) == (UNKNOWN)
    assert verdict_of(location="Remote, Europe", description="A W-2 position.") == EXCLUDED


def test_confirmed_geography_names_no_route() -> None:
    assessment = assess_eligibility(posting(location="Türkiye (Remote)"))
    assert assessment.verdict == CONFIRMED
    assert assessment.routes == ()
    # A route is recorded only when its own phrase fired.
    with_route = assess_eligibility(
        posting(location="Türkiye (Remote)", description="Engaged as an independent contractor.")
    )
    assert with_route.routes == ("contractor",)


def test_a_turkiye_location_with_a_restriction_is_possible() -> None:
    assessment = assess_eligibility(
        posting(location="Türkiye (Remote)", description="You must be based in the UK.")
    )
    assert assessment.verdict == POSSIBLE


# --- separation from fit and screening ------------------------------------------------


def _screen(jobs: list[NormalizedJob]) -> tuple[list[dict[str, str]], dict[str, int]]:
    result = screen_jobs(
        jobs,
        candidate_cfg=candidate_cfg(),
        hold_companies=set(),
        indexes=build_tracker_indexes([]),
        source_seen={},
        cache_descriptions=False,
    )
    return result.new_tracker_rows, result.rejected_counts


def _screening_batch() -> list[NormalizedJob]:
    return [
        posting(location="Türkiye (Remote)", url="https://jobs.example.com/acme/1", company="A"),
        posting(
            location="Remote, Europe",
            description="TypeScript React. You must be based in the UK.",
            url="https://jobs.example.com/acme/2",
            company="B",
        ),
        posting(
            location="Remote, Europe",
            description="React. A B2B contract role, anywhere in EMEA.",
            url="https://jobs.example.com/acme/3",
            company="C",
        ),
        posting(location="Hybrid, Berlin", url="https://jobs.example.com/acme/4", company="D"),
    ]


def test_eligibility_changes_no_score_and_no_gate(env: Path) -> None:
    with_assessor = _screen(_screening_batch())

    def stub(job: NormalizedJob) -> EligibilityAssessment:
        return EligibilityAssessment(verdict=UNKNOWN, checked_at="2026-10-11T00:00:00Z")

    with patch("harrier.screening.pipeline.assess_eligibility", stub):
        stubbed = _screen(_screening_batch())

    def comparable(rows: list[dict[str, str]]) -> list[dict[str, str]]:
        return [{k: v for k, v in row.items() if k != "added_at"} for row in rows]

    assert comparable(with_assessor[0]) == comparable(stubbed[0])
    assert with_assessor[1] == stubbed[1]
    assert len(with_assessor[0]) == 3


def test_eligibility_version_is_not_the_policy_version(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = candidate_cfg()
    before = (eligibility_version(), policy_version(cfg))
    monkeypatch.setattr(eligibility, "TR_NAMED", (r"\bturkiye\b",))
    after = (eligibility_version(), policy_version(cfg))
    assert after[0] != before[0]
    assert after[1] == before[1]


def test_eligibility_is_not_a_learned_feature() -> None:
    assert FEATURE_ORDER == (
        "skill_signal",
        "preferred_signal",
        "title_fit",
        "frontend_share",
        "explicit_emea_remote",
        "years_gap",
    )


def test_an_excluded_row_is_not_reordered(conn: Connection, scope: Scope) -> None:
    first = new_row(conn, scope, number=1, fit_score="90")
    second = new_row(conn, scope, number=2, fit_score="40")
    order = [row["id"] for row in next_up(conn, scope)]
    record_eligibility(
        conn,
        scope,
        first,
        EligibilityAssessment(
            verdict=EXCLUDED,
            evidence_url="https://jobs.example.com/acme/1",
            excerpt="You must be based in the UK.",
            field="description",
            checked_at="2026-10-11T00:00:00Z",
        ),
    )
    assert [row["id"] for row in next_up(conn, scope)] == order
    assert order.index(str(first)) < order.index(str(second))


# --- write path and precedence --------------------------------------------------------


def _greenhouse_feeds(conn: object = None, *, scope: str = "default") -> dict[str, list[str]]:
    return {"greenhouse": ["https://boards.greenhouse.io/acme"], "ashby": [], "lever": []}


def test_discovery_assesses_in_the_row_transaction(
    conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = make_normalized_job(
        source="greenhouse",
        company="Acme Widgets",
        title="Senior Frontend Engineer",
        location="Türkiye (Remote)",
        url="https://boards.greenhouse.io/acme/jobs/1",
        description="TypeScript and React, remote.",
        external_id="acme-1",
    )
    monkeypatch.setattr(discovery_module, "load_ats_feeds", _greenhouse_feeds)

    def fetch(url: str) -> list[NormalizedJob]:
        return [found]

    def send(message: str) -> int:
        return 0

    monkeypatch.setattr(discovery_module, "fetch_greenhouse_jobs", fetch)
    monkeypatch.setattr(discovery_module, "send_telegram_message", send)
    scope = default_scope(conn)
    run_discovery(conn, scope, DiscoveryOptions(only_sources=frozenset({"greenhouse"})))
    (job,) = (row for row in conn.execute("SELECT id FROM jobs").fetchall())
    history = eligibility_history(conn, scope, int(job[0]))
    assert [(entry.origin, entry.verdict) for entry in history] == [("rule", CONFIRMED)]

    # A failure writing the assessment leaves no row either. The store error
    # a failed write raises on either store (spec 112).
    def fail(*args: object) -> None:
        raise StoreDatabaseError("disk full")

    monkeypatch.setattr("harrier.tracker.store._insert_assessment", fail)
    with pytest.raises(StoreDatabaseError, match="disk full"):
        add_job(
            conn,
            {"company": "Acme Widgets", "title": "Second", "url": "https://x.example/2"},
            scope=scope,
            eligibility=assess_eligibility(posting(url="https://x.example/2")),
        )
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def _an_academic_row(conn: Connection) -> tuple[Scope, dict[str, str]]:
    """A row added by hand to an academic track. The store gives the track
    its id: the tenant role may not write one (spec 105; spec 126)."""
    add_track(conn, ACADEMIC, "academic", "PhD search")
    academic = resolve_scope(conn, ACADEMIC)
    result, row = add_manually(
        conn,
        academic,
        company="Example University",
        title="Doctoral position",
        location="Türkiye",
        url="https://uni.example.edu/phd/1",
    )
    assert result.status == "added" and row is not None
    return academic, row


def test_the_academic_kind_is_not_assessed(conn: Connection) -> None:
    academic, row = _an_academic_row(conn)
    assert conn.execute("SELECT count(*) FROM job_eligibility").fetchone()[0] == 0
    with pytest.raises(Exception, match="not available on track"):
        set_eligibility(conn, academic, str(row["id"]), verdict=UNKNOWN)


@pytest.mark.sqlite_only("drives the CLI, which refuses a Postgres URL until spec 127")
def test_the_cli_refuses_eligibility_on_the_academic_kind(conn: Connection) -> None:
    """The command half of `test_the_academic_kind_is_not_assessed`, which
    runs on both stores without it (spec 126)."""
    _, row = _an_academic_row(conn)
    assert main(["--track", ACADEMIC, "eligibility", "show", str(row["id"])]) == 2


def test_a_manual_add_is_assessed(conn: Connection, scope: Scope) -> None:
    result, row = add_manually(
        conn,
        scope,
        company="Acme Widgets",
        title="Senior Frontend Engineer",
        location="Türkiye (Remote)",
        url="https://jobs.example.com/acme/9",
    )
    assert result.status == "added" and row is not None
    view = eligibility_of(conn, scope, int(row["id"]))
    assert view.current is not None and view.current.verdict == CONFIRMED


@pytest.mark.sqlite_only(
    "drives the CLI and the API, which refuse a Postgres URL until specs 127 and 104"
)
def test_a_firm_verdict_needs_evidence(conn: Connection, scope: Scope, client: TestClient) -> None:
    job_id = new_row(conn, scope)
    for verdict in (CONFIRMED, EXCLUDED):
        assert main(["eligibility", "set", str(job_id), "--verdict", verdict]) == 2
        response = client.post(
            f"/tracker/{job_id}/eligibility", json={"verdict": verdict}, headers=auth()
        )
        assert response.status_code == 409
        assert f"a {verdict} verdict needs its evidence" in response.json()["detail"]
    # Possible and unknown may stand without evidence.
    assert main(["eligibility", "set", str(job_id), "--verdict", POSSIBLE]) == 0
    assert eligibility_of(conn, scope, job_id).current is not None


def _candidate_confirms(conn: Connection, scope: Scope, job_id: int) -> None:
    set_eligibility(
        conn,
        scope,
        str(job_id),
        verdict=CONFIRMED,
        routes=("contractor",),
        evidence_url="https://careers.example.com/acme/hiring-countries",
        excerpt="We hire contractors in Türkiye.",
        checked_at="2026-10-10",
    )


def test_the_candidate_stays_current_over_later_rules(conn: Connection, scope: Scope) -> None:
    job_id = new_row(
        conn, scope, location="Remote, Europe", description="You must be based in the UK."
    )
    _candidate_confirms(conn, scope, job_id)

    report = reassess_eligibility(conn, scope, apply=True)
    assert (report.assessed, report.under_candidate, report.disagrees) == (1, 1, 1)

    view = eligibility_of(conn, scope, job_id)
    assert view.current is not None and view.current.origin == "candidate"
    assert view.current.verdict == CONFIRMED
    assert view.current.field == "external"
    assert view.latest_rule is not None and view.latest_rule.verdict == EXCLUDED
    assert view.latest_rule.version == eligibility_version()
    assert view.latest_rule.description_sha256
    assert view.latest_rule.excerpt == "You must be based in the UK."
    assert view.disagrees is True


def test_a_rule_that_agrees_does_not_disagree(conn: Connection, scope: Scope) -> None:
    job_id = new_row(conn, scope, location="Türkiye (Remote)", description="React.")
    set_eligibility(
        conn,
        scope,
        str(job_id),
        verdict=CONFIRMED,
        evidence_url="https://jobs.example.com/acme/1",
        excerpt="Türkiye (Remote)",
    )
    report = reassess_eligibility(conn, scope, apply=True)
    assert (report.under_candidate, report.disagrees) == (1, 0)
    assert eligibility_of(conn, scope, job_id).disagrees is False


def test_only_the_candidate_changes_a_candidate_verdict(conn: Connection, scope: Scope) -> None:
    job_id = new_row(conn, scope, description="You must be based in the UK.")
    _candidate_confirms(conn, scope, job_id)
    record_eligibility(
        conn,
        scope,
        job_id,
        EligibilityAssessment(verdict=POSSIBLE, checked_at="2026-10-11T00:00:00Z"),
    )
    current = eligibility_of(conn, scope, job_id).current
    assert current is not None and current.verdict == CONFIRMED

    set_eligibility(conn, scope, str(job_id), verdict=UNKNOWN)
    current = eligibility_of(conn, scope, job_id).current
    assert current is not None and (current.origin, current.verdict) == ("candidate", UNKNOWN)
    # The earlier verdicts stay in the history.
    assert [entry.verdict for entry in eligibility_history(conn, scope, job_id)] == [
        UNKNOWN,
        POSSIBLE,
        CONFIRMED,
    ]


def test_eligibility_is_not_a_status(conn: Connection, scope: Scope) -> None:
    job_id = new_row(conn, scope)
    before = get_job(conn, scope, job_id)
    events = list_events(conn, scope, job_id)
    _candidate_confirms(conn, scope, job_id)
    after = get_job(conn, scope, job_id)
    for column in ("status", "rejection_reason", "next_action", "fit_score", "signals"):
        assert after[column] == before[column], column
    assert list_events(conn, scope, job_id) == events


def test_eligibility_history_is_append_only(
    conn: Connection, scope: Scope, dual_store: DualStore
) -> None:
    job_id = new_row(conn, scope)
    _candidate_confirms(conn, scope, job_id)
    for statement in (
        "UPDATE job_eligibility SET verdict = 'excluded'",
        "DELETE FROM job_eligibility",
    ):
        if dual_store.dialect == "postgres":
            # The tenant role holds no UPDATE or DELETE on job_eligibility,
            # so the owner is refused at the privilege. The append-only
            # triggers refuse a role that holds it: the migrating role, which
            # owns the table.
            with pytest.raises(StoreDatabaseError, match="permission denied"):
                conn.execute(statement)
            with pytest.raises(StoreIntegrityError, match="job_eligibility is append-only"):
                run_as_migrator(dual_store.url, statement)
        else:
            with pytest.raises(StoreIntegrityError, match="job_eligibility is append-only"):
                conn.execute(statement)


def test_the_write_path_refuses_what_it_cannot_store(conn: Connection, scope: Scope) -> None:
    job_id = new_row(conn, scope)
    for bad, words in (
        (EligibilityAssessment(verdict="maybe", checked_at="2026-10-11T00:00:00Z"), "verdict"),
        (
            EligibilityAssessment(
                verdict=UNKNOWN, routes=("payroll",), checked_at="2026-10-11T00:00:00Z"
            ),
            "route",
        ),
        (
            EligibilityAssessment(
                verdict=UNKNOWN, excerpt="a" * 301, checked_at="2026-10-11T00:00:00Z"
            ),
            "excerpt",
        ),
        (
            EligibilityAssessment(
                verdict=UNKNOWN, evidence_url="ftp://x", checked_at="2026-10-11T00:00:00Z"
            ),
            "http",
        ),
        (EligibilityAssessment(verdict=UNKNOWN, checked_at="yesterday"), "checked_at"),
    ):
        with pytest.raises(EligibilityError, match=words):
            record_eligibility(conn, scope, job_id, bad)
    with pytest.raises(Exception, match="in the future"):
        set_eligibility(conn, scope, str(job_id), verdict=UNKNOWN, checked_at="2999-01-01")


# --- reassess and backfill -----------------------------------------------------------


@pytest.mark.sqlite_only("drives the API, which refuses a Postgres URL until spec 104")
def test_migration_backfills_nothing(conn: Connection, scope: Scope, client: TestClient) -> None:
    new_row(conn, scope, location="Türkiye (Remote)", description="React.")
    assert conn.execute("SELECT count(*) FROM job_eligibility").fetchone()[0] == 0
    (job,) = client.get("/jobs").json()
    assert job["eligibility"]["verdict"] == "unknown"
    assert job["eligibility"]["origin"] == "none"
    assert job["eligibility"]["disagrees"] is False


def test_reassess_touches_only_what_it_may(conn: Connection, scope: Scope) -> None:
    open_row = new_row(conn, scope, number=1, location="Türkiye (Remote)", description="React.")
    no_text = new_row(conn, scope, number=2)
    rejected = new_row(conn, scope, number=3, description="You must be based in the UK.")
    change_status(conn, scope, str(rejected), "reject", reason="not a fit")

    dry = reassess_eligibility(conn, scope)
    assert (dry.examined, dry.assessed, dry.no_description, dry.rejected_skipped) == (3, 1, 1, 1)
    assert dict(dry.by_verdict) == {CONFIRMED: 1}
    assert conn.execute("SELECT count(*) FROM job_eligibility").fetchone()[0] == 0

    applied = reassess_eligibility(conn, scope, apply=True)
    assert applied.assessed == 1
    assert [entry.verdict for entry in eligibility_history(conn, scope, open_row)] == [CONFIRMED]
    assert eligibility_history(conn, scope, no_text) == []

    again = reassess_eligibility(conn, scope, apply=True)
    assert (again.assessed, again.unchanged) == (0, 1)


def test_a_row_rejected_during_reassess_is_skipped_not_fatal(
    conn: Connection, scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejection that lands after `reassess` read its rows is a skip. It
    stopped the run partway, with the rows before it written and the report
    lost (post-merge review of PR #242)."""
    import harrier.tracker.actions as actions_module
    from harrier.tracker.store import list_jobs

    first = new_row(conn, scope, number=1, location="Türkiye (Remote)", description="React.")
    second = new_row(conn, scope, number=2, location="Türkiye (Remote)", description="React.")
    third = new_row(conn, scope, number=3, location="Türkiye (Remote)", description="React.")
    read_before_the_rejection = list_jobs(conn, scope)
    change_status(conn, scope, str(second), "reject", reason="not a fit")

    def stale_rows(*args: object) -> list[dict[str, str]]:
        return read_before_the_rejection

    monkeypatch.setattr(actions_module, "list_jobs", stale_rows)

    report = reassess_eligibility(conn, scope, apply=True)

    assert (report.assessed, report.rejected_skipped) == (2, 1)
    assert dict(report.by_verdict) == {CONFIRMED: 2}
    assert eligibility_history(conn, scope, second) == []
    for job_id in (first, third):
        assert [entry.verdict for entry in eligibility_history(conn, scope, job_id)] == [CONFIRMED]


def test_a_rejected_row_is_not_added_with_a_rule_assessment(conn: Connection, scope: Scope) -> None:
    """The rule holds on the first write too: `add_job` refuses a rule
    assessment for a row it would store as rejected, and stores neither
    (post-merge review of PR #242)."""
    fields = {
        "company": "Acme Widgets",
        "title": "Senior Frontend Engineer",
        "url": "https://jobs.example.com/acme/7",
        "status": "rejected",
    }
    with pytest.raises(EligibilityError, match="rejected"):
        add_job(
            conn,
            fields,
            scope=scope,
            eligibility=assess_eligibility(posting(url="https://jobs.example.com/acme/7")),
        )
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    # The candidate's own verdict on a rejected row is theirs to record.
    candidate = EligibilityAssessment(
        verdict=UNKNOWN, origin="candidate", checked_at="2026-10-11T00:00:00Z"
    )
    job_id = add_job(conn, fields, scope=scope, eligibility=candidate)
    assert [entry.origin for entry in eligibility_history(conn, scope, job_id)] == ["candidate"]


def test_reassess_never_touches_a_rejected_row(conn: Connection, scope: Scope) -> None:
    rejected = new_row(conn, scope, description="You must be based in the UK.")
    change_status(conn, scope, str(rejected), "reject", reason="not a fit")
    reassess_eligibility(conn, scope, apply=True)
    assert eligibility_history(conn, scope, rejected) == []
    # Nor does any other path assess one by rule.
    with pytest.raises(EligibilityError, match="rejected"):
        record_eligibility(
            conn,
            scope,
            rejected,
            EligibilityAssessment(verdict=UNKNOWN, checked_at="2026-10-11T00:00:00Z"),
        )


def test_reassess_reads_version_and_description(
    conn: Connection, scope: Scope, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = new_row(conn, scope, location="Remote", description="React.")
    reassess_eligibility(conn, scope, apply=True)

    save_description_cache("https://jobs.example.com/acme/1", "React. You must be based in the UK.")
    changed = reassess_eligibility(conn, scope, apply=True)
    assert changed.assessed == 1
    latest = eligibility_of(conn, scope, job_id).latest_rule
    assert latest is not None and latest.verdict == EXCLUDED

    monkeypatch.setattr(eligibility, "REGION_PATTERNS", (r"\bworldwide\b",))
    assert reassess_eligibility(conn, scope).assessed == 1
    assert len(eligibility_history(conn, scope, job_id)) == 2


# --- surfaces ------------------------------------------------------------------------


@pytest.mark.sqlite_only("drives the API, which refuses a Postgres URL until spec 104")
def test_the_api_reads_what_the_store_holds(
    conn: Connection, scope: Scope, client: TestClient
) -> None:
    job_id = new_row(conn, scope, description="You must be based in the UK.")
    response = client.post(
        f"/tracker/{job_id}/eligibility",
        json={
            "verdict": "confirmed",
            "routes": ["contractor"],
            "evidence_url": "https://careers.example.com/acme/hiring-countries",
            "excerpt": "We hire contractors in Türkiye.",
            "checked_at": "2026-10-10",
        },
        headers=auth(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["origin"] == "candidate"
    assert response.json()["checked_at"] == "2026-10-10T00:00:00Z"

    report = client.post("/ops/eligibility-reassess", json={"apply": True}, headers=auth())
    assert report.status_code == 200
    assert report.json()["disagrees"] == 1

    (job,) = client.get("/jobs").json()
    assert job["eligibility"]["verdict"] == "confirmed"
    assert job["eligibility"]["routes"] == ["contractor"]
    assert job["eligibility"]["disagrees"] is True

    history = client.get(f"/tracker/{job_id}/eligibility").json()
    assert [(entry["origin"], entry["verdict"]) for entry in history] == [
        ("rule", "excluded"),
        ("candidate", "confirmed"),
    ]
    assert client.get("/tracker/999999/eligibility").status_code == 404


@pytest.mark.sqlite_only(
    "drives the CLI and the API, which refuse a Postgres URL until specs 127 and 104"
)
def test_the_cli_and_the_api_call_the_same_eligibility_functions(
    conn: Connection, scope: Scope, client: TestClient
) -> None:
    job_id = new_row(conn, scope)
    real = eligibility_of(conn, scope, job_id)
    with patch("harrier.tracker.actions.set_eligibility", return_value=real) as action:
        main(["eligibility", "set", str(job_id), "--verdict", "possible", "--route", "eor"])
        cli_call = action.call_args
    with patch("harrier.tracker.actions.set_eligibility", return_value=real) as action:
        client.post(
            f"/tracker/{job_id}/eligibility",
            json={"verdict": "possible", "routes": ["eor"]},
            headers=auth(),
        )
        api_call = action.call_args
    assert cli_call is not None and api_call is not None
    assert cli_call.args[1:] == api_call.args[1:]
    assert cli_call.kwargs == api_call.kwargs

    with patch("harrier.tracker.actions.reassess_eligibility") as action:
        action.return_value = reassess_eligibility(conn, scope)
        main(["eligibility", "reassess"])
        client.post("/ops/eligibility-reassess", json={}, headers=auth())
    assert [call.kwargs for call in action.call_args_list] == [{"apply": False}] * 2


def test_the_contract_names_the_schema_values() -> None:
    """The API's literals and the schema's tuples are one list."""
    from typing import get_args

    assert get_args(app_module.EligibilityVerdict) == schema.ELIGIBILITY_VERDICTS
    assert get_args(app_module.EligibilityRoute) == schema.ELIGIBILITY_ROUTES
    assert get_args(app_module.EligibilityField) == ("", *schema.ELIGIBILITY_FIELDS)
    # `origin` too: a stored assessment's, and a row's, which adds `none`
    # for a row with no assessment (post-merge review of PR #242).
    entry_origin = app_module.EligibilityEntryOut.model_fields["origin"].annotation
    row_origin = app_module.EligibilityOut.model_fields["origin"].annotation
    assert get_args(entry_origin) == schema.ELIGIBILITY_ORIGINS
    assert get_args(row_origin) == (*schema.ELIGIBILITY_ORIGINS, "none")


def test_excerpts_are_not_logged(
    conn: Connection, scope: Scope, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "We hire contractors in Türkiye."
    job_id = new_row(conn, scope, description=f"React. {secret}")
    with caplog.at_level(logging.DEBUG):
        _candidate_confirms(conn, scope, job_id)
        reassess_eligibility(conn, scope, apply=True)
    assert caplog.records, "nothing was logged, so this proves nothing"
    for record in caplog.records:
        if record.levelno > logging.DEBUG:
            assert secret not in record.getMessage()
        assert "contractors in" not in record.getMessage()
