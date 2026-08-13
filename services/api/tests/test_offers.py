"""Behavior pins for offer evaluation and the batch driver (spec 015).

The old repo had no tests for these scripts; every pin here is new, run
on the synthetic fixtures (resume bundle, candidate document, story
seeds) so no personal content enters the repo."""

import json
import sqlite3
from pathlib import Path
from typing import cast

import pytest

import harrier.offers.batch as batch_module
import harrier.offers.evaluate as evaluate_module
from harrier.db import connect, data_dir
from harrier.llm import LLMClientError
from harrier.offers import (
    BatchOptions,
    EvaluationError,
    EvaluationResult,
    Verdict,
    build_system_prompt,
    evaluate_offer,
    evaluate_prospects,
    load_story_bank,
    parse_json_response,
    parse_verdict,
    report_path_for,
)
from harrier.offers.stories import STORY_BANK_LIMIT, capture_stories
from harrier.profile.store import put_document
from harrier.tracker import add_job, get_job

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    conn = connect()
    put_document(
        conn,
        "resume_data",
        "resume-content.json",
        "json",
        (REPO_ROOT / "config" / "resume-content.example.json").read_text(encoding="utf-8"),
    )
    put_document(conn, "resume_truth", "truth.md", "markdown", "- Verified fact one.\n")
    put_document(
        conn,
        "candidate",
        "candidate.json",
        "json",
        json.dumps(
            {
                "candidate": {"name": "Deniz Örnek"},
                "targets": {"titles": ["Senior Frontend Engineer"]},
                "compensation": {"salary_min_eur": 50000, "salary_target_eur": 60000},
            }
        ),
    )
    put_document(
        conn,
        "story_seeds",
        "story-seeds.json",
        "json",
        (REPO_ROOT / "config" / "story-seeds.example.json").read_text(encoding="utf-8"),
    )
    return conn


def ai_response(verdict: str = "apply", confidence: object = 0.7) -> str:
    return json.dumps(
        {
            "block_a": {
                "archetype": "product_engineer",
                "archetype_rationale": "Product-facing frontend work.",
                "domain": "devtools",
                "seniority_match": "aligned",
                "remote_confirmed": True,
                "comp_estimate": "not disclosed",
                "tldr": "A solid product engineering role.",
            },
            "block_b": [
                {
                    "jd_requirement": "TypeScript",
                    "evidence": "Verified fact one.",
                    "gap": False,
                    "mitigation": None,
                }
            ],
            "block_c": {"selling_points": ["Frontend ownership"], "honest_gaps": []},
            "block_d": {
                "profile_angle": "Product-focused frontend engineer.",
                "cv_adjustments": ["Lead with product delivery."],
                "headline_suggestion": "Senior Frontend Engineer",
            },
            "block_e": [
                {
                    "story_id": "architecture_migration",
                    "theme": "Architecture",
                    "jd_hook": "ownership",
                    "opening_line": "I led a migration.",
                    "star_r": {
                        "situation": "s",
                        "task": "t",
                        "action": "a",
                        "result": "r",
                        "reflection": "x",
                    },
                }
            ],
            "block_f": {
                "verdict": verdict,
                "confidence": confidence,
                "reason": "Solid fit on stack and archetype.",
                "deal_breakers": [],
            },
        }
    )


# ---------------------------------------------------------------------------
# Prompt assembly and the verdict contract
# ---------------------------------------------------------------------------


def test_system_prompt_is_assembled_from_data(db: sqlite3.Connection) -> None:
    prompt = build_system_prompt(db)
    assert "Deniz Örnek" in prompt
    assert "Senior Frontend Engineer" in prompt
    assert "50,000-60,000" in prompt
    assert "below 40k EUR" in prompt
    assert "{name}" not in prompt
    assert "{comp_floor}" not in prompt


def test_parse_json_response_strips_markdown_fences() -> None:
    payload = parse_json_response('```json\n{"block_f": {"verdict": "apply"}}\n```')
    assert payload is not None
    assert cast("dict[str, object]", payload["block_f"])["verdict"] == "apply"


def test_parse_verdict_contract_defends_against_malformed_output() -> None:
    unknown = parse_verdict({"block_f": {"verdict": "yolo", "confidence": 0.99}})
    assert unknown.verdict == "borderline"
    non_numeric = parse_verdict({"block_f": {"verdict": "skip", "confidence": "high"}})
    assert non_numeric.confidence == 0.0
    out_of_range = parse_verdict({"block_f": {"verdict": "skip", "confidence": 3.5}})
    assert out_of_range.confidence == 0.0


# ---------------------------------------------------------------------------
# Single evaluation
# ---------------------------------------------------------------------------


def test_evaluate_offer_writes_report_with_verdict_first(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_generate(system_prompt: str, user_input: str) -> str:
        return ai_response()

    monkeypatch.setattr(evaluate_module, "generate_text", fake_generate)
    result = evaluate_offer(db, "Example Co", "Senior Frontend Engineer", "https://x.test", "JD")
    report = result.report_path.read_text(encoding="utf-8")
    assert report.index("## F: Verdict") < report.index("## A: Role Classification")
    assert "**APPLY** (confidence=0.70)" in report
    assert result.verdict.verdict == "apply"
    bank = load_story_bank(db)
    assert [entry["story_id"] for entry in bank] == ["architecture_migration"]


def test_evaluate_offer_requires_jd(db: sqlite3.Connection) -> None:
    with pytest.raises(EvaluationError, match="no job description"):
        evaluate_offer(db, "Example Co", "Role", "https://x.test", "   ")


def test_evaluate_offer_wraps_ai_failure(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(system_prompt: str, user_input: str) -> str:
        raise LLMClientError("backend unavailable")

    monkeypatch.setattr(evaluate_module, "generate_text", boom)
    with pytest.raises(EvaluationError, match="AI request failed"):
        evaluate_offer(db, "Example Co", "Role", "https://x.test", "JD")


def test_story_bank_dedupes_and_respects_bound(db: sqlite3.Connection) -> None:
    first = [{"story_id": "a", "theme": "one"}]
    updated = [{"story_id": "a", "theme": "two"}]
    capture_stories(db, cast("list[dict[str, object]]", first), "Co", "Role")
    capture_stories(db, cast("list[dict[str, object]]", updated), "Co", "Role")
    bank = load_story_bank(db)
    assert len(bank) == 1
    assert bank[0]["theme"] == "two"

    many = [{"story_id": f"s{i}", "theme": "bulk"} for i in range(STORY_BANK_LIMIT + 20)]
    total = capture_stories(db, cast("list[dict[str, object]]", many), "Co", "Role")
    assert total == STORY_BANK_LIMIT


# ---------------------------------------------------------------------------
# Batch driver: the auto-reject gate
# ---------------------------------------------------------------------------


def add_prospect(conn: sqlite3.Connection, index: int = 1) -> int:
    return add_job(
        conn,
        {
            "company": f"Batch Co {index}",
            "title": "Senior Frontend Engineer",
            "location": "Remote, Europe",
            "url": f"https://example.test/batch/{index}",
            "source": "greenhouse",
            "status": "prospect",
        },
    )


def fake_result(company: str, role: str, verdict: Verdict) -> EvaluationResult:
    path = report_path_for(company, role)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# report\n", encoding="utf-8")
    return EvaluationResult(report_path=path, verdict=verdict, data={})


def test_skip_verdict_above_threshold_rejects_only_with_apply(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = add_prospect(db)
    skip_verdict = Verdict(
        verdict="skip", confidence=0.9, reason="not remote", deal_breakers=("onsite",)
    )

    def fake_evaluate(
        conn: sqlite3.Connection,
        company: str,
        role: str,
        url: str,
        jd_text: str,
        output_dir: Path | None = None,
    ) -> EvaluationResult:
        return fake_result(company, role, skip_verdict)

    monkeypatch.setattr(batch_module, "evaluate_offer", fake_evaluate)
    audit_path = data_dir() / "evaluations" / "audit.jsonl"

    dry = evaluate_prospects(db, BatchOptions(apply=False))
    assert dry.would_reject == 1
    assert dry.auto_rejected == 0
    assert get_job(db, job_id)["status"] == "prospect"
    assert not audit_path.exists()

    live = evaluate_prospects(db, BatchOptions(apply=True, refresh=True))
    assert live.auto_rejected == 1
    row = get_job(db, job_id)
    assert row["status"] == "rejected"
    assert row["rejection_reason"].startswith("ai-evaluation:")
    entries = [json.loads(line) for line in audit_path.read_text().splitlines()]
    assert len(entries) == 1
    assert entries[0]["verdict"] == "skip"
    assert entries[0]["job_id"] == job_id
    assert entries[0]["threshold"] == 0.8


def test_existing_report_gates_rerun_unless_refresh(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_prospect(db)
    apply_verdict = Verdict(verdict="apply", confidence=0.7, reason="fit", deal_breakers=())
    calls = {"count": 0}

    def fake_evaluate(
        conn: sqlite3.Connection,
        company: str,
        role: str,
        url: str,
        jd_text: str,
        output_dir: Path | None = None,
    ) -> EvaluationResult:
        calls["count"] += 1
        return fake_result(company, role, apply_verdict)

    monkeypatch.setattr(batch_module, "evaluate_offer", fake_evaluate)
    first = evaluate_prospects(db, BatchOptions())
    assert first.processed == 1
    second = evaluate_prospects(db, BatchOptions())
    assert second.processed == 0
    assert second.skipped_existing == 1
    third = evaluate_prospects(db, BatchOptions(refresh=True))
    assert third.processed == 1
    assert calls["count"] == 2


def test_invalid_confidence_never_clears_the_threshold(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = add_prospect(db)
    # parse_verdict has already coerced an invalid confidence to 0.0 by the
    # time the batch driver sees it; the gate must therefore never fire.
    malformed = parse_verdict({"block_f": {"verdict": "skip", "confidence": "very sure"}})

    def fake_evaluate(
        conn: sqlite3.Connection,
        company: str,
        role: str,
        url: str,
        jd_text: str,
        output_dir: Path | None = None,
    ) -> EvaluationResult:
        return fake_result(company, role, malformed)

    monkeypatch.setattr(batch_module, "evaluate_offer", fake_evaluate)
    summary = evaluate_prospects(db, BatchOptions(apply=True))
    assert summary.auto_rejected == 0
    assert get_job(db, job_id)["status"] == "prospect"


def test_borderline_rejected_only_with_include_borderline(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = add_prospect(db)
    borderline = Verdict(verdict="borderline", confidence=0.95, reason="mixed", deal_breakers=())

    def fake_evaluate(
        conn: sqlite3.Connection,
        company: str,
        role: str,
        url: str,
        jd_text: str,
        output_dir: Path | None = None,
    ) -> EvaluationResult:
        return fake_result(company, role, borderline)

    monkeypatch.setattr(batch_module, "evaluate_offer", fake_evaluate)
    plain = evaluate_prospects(db, BatchOptions(apply=True))
    assert plain.auto_rejected == 0
    with_flag = evaluate_prospects(
        db, BatchOptions(apply=True, refresh=True, include_borderline=True)
    )
    assert with_flag.auto_rejected == 1
    assert get_job(db, job_id)["status"] == "rejected"
