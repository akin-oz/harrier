"""The learned fit score (spec 077).

What these hold: the model reads a fixed, deterministic vector; it scores in
plain Python exactly as scikit-learn would; the rules take over whenever it
cannot judge and the row says so; labels come from the candidate's own
decisions and never from a company's; and a model is activated only when it
beats the rules on later rows and has learned that a blocker ranks a posting
down.

Every posting and every export here is synthetic: invented companies,
invented text, generated numbers.
"""

# scikit-learn ships without type information; the unknowns are its alone.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import fnmatch
import json
import math
import random
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest
from sklearn.linear_model import LogisticRegression

import harrier.scoring.model as model_module
import harrier.scoring.train as train_module
from harrier.atomicio import write_bytes_atomic
from harrier.db import connect
from harrier.scoring.export import export_features, exports_dir
from harrier.scoring.features import FEATURE_ORDER, NUMERIC_FEATURES, extract, required_years
from harrier.scoring.labels import (
    ACTOR_UNKNOWN,
    BLOCKED,
    DESCRIPTION_CHANGED,
    DESCRIPTION_MISSING,
    SYSTEM_CLOSED,
    UNDECIDED,
    Labelled,
    label_job,
)
from harrier.scoring.model import (
    MODEL_SCORE_BOUNDS,
    NO_MODEL,
    ModelInvalidError,
    active_model_path,
    dump_model,
    fit_score_for,
    model_blocker_penalty,
    model_document,
    model_identity,
    parse_model,
)
from harrier.scoring.train import (
    FORBIDDEN_FEATURES,
    ExportError,
    Row,
    models_dir,
    p92_from,
    read_export,
    split,
    train,
)
from harrier.screening import rules
from harrier.screening.descriptions import load_cached_description, save_description_cache
from harrier.screening.normalized import NormalizedJob, make_normalized_job
from harrier.screening.pipeline import TrackerIndexes, screen_jobs
from harrier.screening.policy import policy_version
from harrier.tracker.actions import change_status, record_company_outcome
from harrier.tracker.store import add_job, backfill_events, get_job, list_events, set_status
from harrier_cli.main import main

ROOT = Path(__file__).resolve().parents[3]

# Long enough to be judged (`MIN_DESCRIPTION_LENGTH_FOR_SCORING`), and shared
# by postings that should differ only in what a test changes.
BODY = (
    "We build developer tools with TypeScript and React. You will own testing "
    "and delivery across the product, with a small team that cares about "
    "observability and performance."
)


@pytest.fixture
def cfg() -> dict[str, object]:
    path = ROOT / "config" / "candidate.example.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _posting(title: str, location: str, description: str) -> NormalizedJob:
    return make_normalized_job(
        source="greenhouse",
        company="Quillfeather Labs",
        title=title,
        location=location,
        url="https://boards.example.com/quillfeather/1",
        description=description,
    )


# Signed, distinct weights. A stand-in for a trained model: it proves the
# features fire and the scorer applies them. No blocker weights: blockers
# floor a posting by rule (spec 081).
FIXTURE_COEFFICIENTS: dict[str, float] = {
    "skill_signal": 2.0,
    "preferred_signal": 0.5,
    "title_fit": 1.1,
    "frontend_share": 0.9,
    "explicit_emea_remote": 1.3,
    "years_gap": -0.7,
}
FIXTURE_P92: dict[str, float] = {"skill_signal": 30.0, "preferred_signal": 12.0, "years_gap": 5.0}


def _document(
    coefficients: dict[str, float] | None = None, intercept: float = -1.0
) -> dict[str, object]:
    weights = coefficients or FIXTURE_COEFFICIENTS
    return model_document(
        coefficients=[weights[name] for name in FEATURE_ORDER],
        intercept=intercept,
        p92=FIXTURE_P92,
        created_at="2026-10-06",
        training={"rows": 0, "positives": 0, "split_at": "2026-10-06"},
        evaluation={"metric": "average_precision"},
    )


def _activate(document: dict[str, object] | None = None) -> str:
    data = dump_model(document or _document())
    write_bytes_atomic(active_model_path(), data)
    return model_identity(data)


# --- the features -------------------------------------------------------------


def test_us_only_w2_posting_ranks_below_emea_remote_with_same_keywords(
    cfg: dict[str, object],
) -> None:
    """The 151 case, under the learned score: same skills, but one posting
    the candidate cannot take. Floored by rule rather than by a learned
    weight (spec 081), and the row names each blocker and its phrase."""
    _activate()
    us_only = _posting(
        "Senior Frontend Engineer",
        "Remote",
        f"Open to candidates anywhere in the US. W-2 position. {BODY}",
    )
    emea = _posting("Senior Frontend Engineer", "Remote, Europe", f"Remote across Europe. {BODY}")

    blocked, eligible = fit_score_for(us_only, cfg), fit_score_for(emea, cfg)
    assert blocked.score < eligible.score
    assert blocked.reasons[0].startswith("scorer=model:")
    assert 'blocker=us_scope "anywhere in the us"' in blocked.reasons
    assert 'blocker=employment "w-2"' in blocked.reasons
    assert not [entry for entry in eligible.reasons if entry.startswith("blocker=")]


def test_a_blocked_posting_ranks_below_every_unblocked_model_score(
    cfg: dict[str, object],
) -> None:
    """The floor at its tightest: a blocked posting the model is all but
    certain about against an unblocked one it all but rejects (spec 081)."""
    low, high = MODEL_SCORE_BOUNDS
    assert model_blocker_penalty() == high - low + 1
    eager = {name: 0.0 for name in FEATURE_ORDER} | {"skill_signal": 60.0}
    _activate(_document(eager, intercept=-30.0))
    strong = f"TypeScript, React, Vue, Nuxt, Next.js and Node, frontend first. {BODY}"
    blocked = _posting("Senior Frontend Engineer", "Remote", f"W-2 role. {strong}")
    unblocked = _posting(
        "Senior Frontend Engineer",
        "Remote, Europe",
        "We hire across Europe for a role with the team that keeps our offices running, "
        "which the posting describes at length without naming a single technology.",
    )
    blocked_fit, unblocked_fit = fit_score_for(blocked, cfg), fit_score_for(unblocked, cfg)
    assert blocked_fit.reasons[1] == "p=1.00"
    assert unblocked_fit.reasons[1] == "p=0.00"
    assert blocked_fit.score == high - model_blocker_penalty()
    assert unblocked_fit.score == low
    assert blocked_fit.score < unblocked_fit.score


def test_blocked_model_rows_name_their_blockers(cfg: dict[str, object]) -> None:
    identity = _activate()
    job = _posting(
        "Senior Frontend Engineer",
        "Remote",
        f"Open to candidates anywhere in the US. W-2 position. {BODY}",
    )
    reasons = fit_score_for(job, cfg).reasons
    assert reasons[0] == f"scorer=model:{identity}"
    assert reasons[1].startswith("p=")
    blocker_entries = [entry for entry in reasons if entry.startswith("blocker=")]
    assert blocker_entries == ['blocker=us_scope "anywhere in the us"', 'blocker=employment "w-2"']
    # After the contributions, which are all signed feature entries.
    first_blocker = reasons.index(blocker_entries[0])
    assert all(entry[0] in "+-" for entry in reasons[2:first_blocker])


def test_blockers_are_not_features() -> None:
    assert "us_scope" not in FEATURE_ORDER
    assert "employment_blocker" not in FEATURE_ORDER
    # A model written under spec 077's eight-feature order is refused.
    old = _document()
    old["feature_order"] = [*FEATURE_ORDER[:5], "us_scope", "employment_blocker", "years_gap"]
    old["coefficients"] = [0.1] * 8
    with pytest.raises(ModelInvalidError, match="feature_order"):
        parse_model(dump_model(old))


def test_feature_extraction_is_deterministic(cfg: dict[str, object]) -> None:
    """The same job, the same vector: twice here and once in a fresh
    interpreter, which is what lets the export and inference share it."""
    job = _posting(
        "Senior Product Engineer",
        "Remote, Europe",
        f"5+ years of professional experience. Python and React. {BODY}",
    )
    first, second = extract(job, cfg).vector(), extract(job, cfg).vector()
    assert first == second

    script = (
        "import json, sys\n"
        "from harrier.scoring.features import extract\n"
        "payload = json.load(sys.stdin)\n"
        "print(json.dumps(extract(payload['job'], payload['cfg']).vector()))\n"
    )
    fresh = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps({"job": dict(job), "cfg": cfg}),
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(fresh.stdout) == first


@pytest.mark.parametrize(
    "phrase",
    ["Must be based in the EU.", "EU work permit required.", "EU-based contractor welcome."],
)
def test_eu_permit_phrases_are_never_blockers(cfg: dict[str, object], phrase: str) -> None:
    """The product invariant holds under the learned score too: an EU-permit
    phrase never floors a posting (specs 078, 081)."""
    _activate()
    fit = fit_score_for(_posting("Frontend Engineer", "Remote", f"{phrase} {BODY}"), cfg)
    assert fit.reasons[0].startswith("scorer=model:")
    assert not [entry for entry in fit.reasons if entry.startswith("blocker=")]
    assert fit.score >= 0


def test_blocker_features_reuse_the_rule_tables(
    cfg: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One definition of a blocker: a phrase added to spec 078's table reaches
    the rule penalty and the learned score's floor together (spec 081 made
    the floor what the learned score reuses, in place of a feature)."""
    monkeypatch.setattr(
        rules, "US_SCOPE_PATTERNS", (*rules.US_SCOPE_PATTERNS, r"\bstateside applicants only\b")
    )
    job = _posting("Frontend Engineer", "Remote", f"Stateside applicants only. {BODY}")
    _activate()
    floored = fit_score_for(job, cfg)
    assert floored.reasons[0].startswith("scorer=model:")
    assert 'blocker=us_scope "stateside applicants only"' in floored.reasons
    assert floored.score < 0
    assert any(reason.startswith("blocker=us_scope") for reason in rules.score_job(job, cfg)[1])


def test_required_years_reads_the_stated_requirement() -> None:
    assert required_years("5+ years of professional experience with react") == 5
    assert required_years("3-5 years of hands-on experience") == 3
    assert required_years("founded 10 years ago, we value experience") is None
    assert required_years("at least 4 yrs experience and 7 years of experience leading") == 7


@pytest.mark.parametrize(
    "module",
    [
        "harrier.scoring.model",
        "harrier.scoring.features",
        "harrier.scoring.labels",
        "harrier.scoring.export",
        "harrier.scoring.train",
        "harrier.screening",
    ],
)
def test_every_scoring_module_imports_first(module: str) -> None:
    """Scoring reads the screening tables and screening scores through scoring,
    so the order modules load in can make a cycle. Each one must import first,
    in a fresh interpreter, whatever imported it before in this process."""
    subprocess.run([sys.executable, "-c", f"import {module}"], check=True, capture_output=True)


# --- the model file and inference ---------------------------------------------------


def test_model_json_round_trips_and_scores_identically_without_sklearn(tmp_path: Path) -> None:
    # Fitted by scikit-learn, written, read back by the plain-Python scorer.
    rng = np.random.default_rng(7)
    features = rng.uniform(0.0, 1.0, size=(300, len(FEATURE_ORDER)))
    margin = features @ np.linspace(-2.0, 2.0, len(FEATURE_ORDER)) - 0.3
    labels = (rng.uniform(0.0, 1.0, size=300) < 1 / (1 + np.exp(-margin))).astype(int)
    fitted = LogisticRegression(max_iter=1000).fit(features, labels)
    document = model_document(
        coefficients=[float(value) for value in np.asarray(fitted.coef_).reshape(-1)],
        intercept=float(np.asarray(fitted.intercept_).reshape(-1)[0]),
        # Features already lie in [0, 1], so a percentile of 1 leaves them as fitted.
        p92={name: 1.0 for name in NUMERIC_FEATURES},
        created_at="2026-10-06",
        training={"rows": 300, "positives": int(labels.sum()), "split_at": "2026-10-06"},
        evaluation={"metric": "average_precision"},
    )
    data = dump_model(document)
    model = parse_model(data)
    expected = np.asarray(fitted.predict_proba(features)).reshape(-1, 2)[:, 1]
    for row, probability in zip(features, expected, strict=True):
        values = dict(zip(FEATURE_ORDER, (float(value) for value in row), strict=True))
        assert abs(model.probability(values) - float(probability)) < 1e-9
    assert dump_model(document) == data, "the same fit did not produce the same bytes"

    # And with scikit-learn and numpy unimportable, the scorer still scores.
    path = tmp_path / "model.json"
    path.write_bytes(data)
    values = dict(zip(FEATURE_ORDER, (float(value) for value in features[0]), strict=True))
    script = (
        "import json, sys\n"
        "sys.modules['sklearn'] = None\n"
        "sys.modules['numpy'] = None\n"
        "from harrier.scoring.model import parse_model\n"
        "payload = json.load(sys.stdin)\n"
        "model = parse_model(open(payload['path'], 'rb').read())\n"
        "print(repr(model.probability(payload['values'])))\n"
    )
    isolated = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps({"path": str(path), "values": values}),
        capture_output=True,
        text=True,
        check=True,
    )
    assert abs(float(isolated.stdout) - float(expected[0])) < 1e-9


def test_missing_model_falls_back_to_rules_and_records_it(cfg: dict[str, object]) -> None:
    job = _posting("Senior Frontend Engineer", "Remote, Europe", f"Remote across Europe. {BODY}")
    rule_score, rule_reasons = rules.score_job(job, cfg)
    fit = fit_score_for(job, cfg)
    assert fit.score == rule_score
    assert fit.reasons == ["scorer=rules", "fallback=model-missing", *rule_reasons]
    assert fit.version == policy_version(cfg, model=NO_MODEL)


def test_each_fallback_condition_is_recorded(
    cfg: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    judged = _posting("Senior Frontend Engineer", "Remote, Europe", f"Remote, Europe. {BODY}")

    nan_coefficients = {**FIXTURE_COEFFICIENTS, "title_fit": math.nan}
    no_p92 = _document()
    no_p92["p92"] = {"skill_signal": 30.0, "preferred_signal": 12.0}
    wrong_order = _document()
    wrong_order["feature_order"] = list(reversed(FEATURE_ORDER))
    for broken in (
        b"not a model",
        dump_model(wrong_order),
        dump_model(_document(nan_coefficients)),
        dump_model(no_p92),
    ):
        write_bytes_atomic(active_model_path(), broken)
        fit = fit_score_for(judged, cfg)
        assert fit.reasons[:2] == ["scorer=rules", "fallback=model-invalid"], broken[:40]

    _activate()
    thin = _posting("Senior Frontend Engineer", "Remote, Europe", "Remote, Europe.")
    assert fit_score_for(thin, cfg).reasons[:2] == ["scorer=rules", "fallback=description-missing"]

    def broken_extractor(job: NormalizedJob, candidate_cfg: dict[str, object]) -> object:
        raise RuntimeError("synthetic extractor fault")

    monkeypatch.setattr(model_module, "extract", broken_extractor)
    assert fit_score_for(judged, cfg).reasons[:2] == ["scorer=rules", "fallback=extraction-failed"]


def test_scoring_version_names_the_scorer_that_was_used(cfg: dict[str, object]) -> None:
    identity = _activate()
    judged = _posting("Senior Frontend Engineer", "Remote, Europe", f"Remote, Europe. {BODY}")
    thin = _posting("Senior Frontend Engineer", "Remote, Europe", "Remote, Europe.")
    by_model, by_rules = fit_score_for(judged, cfg), fit_score_for(thin, cfg)
    assert by_model.version == policy_version(cfg, model=identity)
    assert by_rules.version == policy_version(cfg, model=NO_MODEL)
    assert by_model.version != by_rules.version


def test_signals_name_the_top_contributions_with_signs(cfg: dict[str, object]) -> None:
    identity = _activate()
    job = _posting(
        "Senior Frontend Engineer",
        "Remote",
        f"Anywhere in the US. 8+ years of experience. {BODY}",
    )
    fit = fit_score_for(job, cfg)
    model = parse_model(active_model_path().read_bytes())
    extraction = extract(job, cfg)
    probability = model.probability(extraction.values)

    assert fit.reasons[0] == f"scorer=model:{identity}"
    assert fit.reasons[1] == f"p={probability:.2f}"
    ranked = sorted(
        (item for item in model.contributions(extraction.values) if item[1] != 0.0),
        key=lambda item: (-abs(item[1]), item[0]),
    )[:5]
    entries = fit.reasons[2 : 2 + len(ranked)]
    assert [entry.split("(")[0] for entry in entries] == [
        f"{'+' if value > 0 else '-'}{name}" for name, value in ranked
    ]
    for entry, (_, value) in zip(entries, ranked, strict=True):
        assert f"({value:+.2f})" in entry
    # The blocker follows the contributions, and the score is floored.
    assert fit.reasons[2 + len(ranked)] == 'blocker=us_scope "anywhere in the us"'
    assert fit.score == round(100 * probability) - model_blocker_penalty()


# --- labels -----------------------------------------------------------------------


@pytest.fixture
def conn() -> sqlite3.Connection:
    return connect()


def _tracked(conn: sqlite3.Connection, index: int, *, described: bool = True) -> int:
    url = f"https://boards.example.com/quillfeather/{index}"
    if described:
        save_description_cache(url, f"Posting {index}. {BODY}")
    return add_job(
        conn,
        {
            "company": f"Quillfeather Labs {index}",
            "title": "Senior Frontend Engineer",
            "location": "Remote, Europe",
            "url": url,
            "added_at": "2026-08-01",
        },
    )


def _label(conn: sqlite3.Connection, job_id: int) -> Labelled | str:
    job = get_job(conn, job_id)
    return label_job(job, list_events(conn, job_id), load_cached_description(job["url"]))


def test_labels_come_from_candidate_decisions(conn: sqlite3.Connection) -> None:
    acted = _tracked(conn, 1)
    change_status(conn, str(acted), "shortlist")
    skipped = _tracked(conn, 2)
    change_status(conn, str(skipped), "reject", reason="missing stack")
    withdrawn = _tracked(conn, 3)
    change_status(conn, str(withdrawn), "applied")
    change_status(conn, str(withdrawn), "reject", reason="hybrid")
    waiting = _tracked(conn, 4)
    unknown = _tracked(conn, 5)
    change_status(conn, str(unknown), "reject", reason="a reason nobody wrote a pattern for")
    undescribed = _tracked(conn, 6, described=False)
    change_status(conn, str(undescribed), "shortlist")

    acted_label = _label(conn, acted)
    assert isinstance(acted_label, Labelled) and acted_label.label == 1 and acted_label.live
    skipped_label = _label(conn, skipped)
    assert isinstance(skipped_label, Labelled) and skipped_label.label == 0
    withdrawn_label = _label(conn, withdrawn)
    assert isinstance(withdrawn_label, Labelled) and withdrawn_label.label == 1
    assert _label(conn, waiting) == UNDECIDED
    assert _label(conn, unknown) == ACTOR_UNKNOWN
    assert _label(conn, undescribed) == DESCRIPTION_MISSING

    # A decision reconstructed by backfill orders by the day the job arrived,
    # because its own time is only an upper bound.
    legacy_url = "https://boards.example.com/quillfeather/legacy"
    save_description_cache(legacy_url, f"Legacy posting. {BODY}")
    conn.execute(
        "INSERT INTO jobs (company, title, url, status, rejection_reason, added_at) "
        "VALUES ('Ironbark Systems', 'Product Engineer', ?, 'rejected', 'hybrid', '2026-03-02')",
        (legacy_url,),
    )
    conn.commit()
    backfill_events(conn)
    legacy = conn.execute("SELECT id FROM jobs WHERE url = ?", (legacy_url,)).fetchone()[0]
    legacy_label = _label(conn, int(legacy))
    assert isinstance(legacy_label, Labelled)
    assert (legacy_label.label, legacy_label.live, legacy_label.decided_at) == (
        0,
        False,
        "2026-03-02",
    )


def test_a_company_outcome_is_never_a_label(conn: sqlite3.Connection) -> None:
    applied = _tracked(conn, 1)
    change_status(conn, str(applied), "applied")
    record_company_outcome(conn, str(applied), "ghosted")
    label = _label(conn, applied)
    # Applied, then ghosted: the candidate wanted it, which is the label.
    assert isinstance(label, Labelled) and label.label == 1

    approached = _tracked(conn, 2)
    record_company_outcome(conn, str(approached), "interview_invited")
    # A recruiter's invitation alone says nothing about the candidate's view.
    assert _label(conn, approached) == UNDECIDED


def test_system_decisions_are_excluded(conn: sqlite3.Connection) -> None:
    closed = _tracked(conn, 1)
    set_status(
        conn,
        closed,
        "rejected",
        rejection_reason="ai-evaluation: onsite",
        reason_code="ai_evaluation",
        actor="system",
    )
    assert _label(conn, closed) == SYSTEM_CLOSED


def test_a_decision_on_a_different_description_is_excluded(conn: sqlite3.Connection) -> None:
    judged = _tracked(conn, 1)
    change_status(conn, str(judged), "shortlist")
    # The cache is keyed by URL and can be rewritten: this text is not the one
    # the candidate judged.
    save_description_cache(get_job(conn, judged)["url"], f"A different posting entirely. {BODY}")
    assert _label(conn, judged) == DESCRIPTION_CHANGED

    result = export_features(conn, today="2026-10-06")
    assert result.excluded[DESCRIPTION_CHANGED] == 1
    header = json.loads(result.path.read_text(encoding="utf-8").splitlines()[0])
    assert header["excluded"][DESCRIPTION_CHANGED] == 1


def test_the_export_carries_no_text(conn: sqlite3.Connection) -> None:
    """An export is a file that travels: ids, labels and numbers only."""
    job_id = _tracked(conn, 1)
    change_status(conn, str(job_id), "shortlist")
    result = export_features(conn, today="2026-10-06")
    text = result.path.read_text(encoding="utf-8")
    for secret in ("Quillfeather", "Senior Frontend Engineer", "boards.example.com", "TypeScript"):
        assert secret not in text, secret


def test_blocked_postings_are_excluded_from_labels(conn: sqlite3.Connection) -> None:
    """The floor ranks a blocked posting, not the model, so it is not a label
    (spec 081)."""
    blocked_url = "https://boards.example.com/quillfeather/blocked"
    save_description_cache(blocked_url, f"Anywhere in the US, W-2 only. {BODY}")
    blocked = add_job(
        conn,
        {
            "company": "Quillfeather Labs 9",
            "title": "Senior Frontend Engineer",
            "location": "Remote",
            "url": blocked_url,
            "added_at": "2026-08-01",
        },
    )
    change_status(conn, str(blocked), "reject", reason="location")
    eligible = _tracked(conn, 1)
    change_status(conn, str(eligible), "shortlist")

    result = export_features(conn, today="2026-10-06")
    assert result.excluded[BLOCKED] == 1
    assert result.rows == 1


# --- training -----------------------------------------------------------------------


def _synthetic_rows(
    n: int, *, seed: int, scenario: str, live_share: float = 1.0
) -> list[dict[str, Any]]:
    """Generated rows for one of two worlds: features that predict the label
    while the baseline is noise ("learnable"), and a baseline that already
    ranks perfectly while the features are noise ("rules_win")."""
    rng = random.Random(seed)
    start = datetime(2026, 8, 1, tzinfo=UTC)
    rows: list[dict[str, Any]] = []
    for index in range(n):
        label = 1 if rng.random() < 0.45 else 0
        values = {name: rng.random() for name in FEATURE_ORDER}
        values["skill_signal"] *= 30
        values["preferred_signal"] *= 12
        values["years_gap"] *= 5
        baseline = rng.uniform(0, 120)
        if scenario == "learnable":
            values["skill_signal"] = rng.uniform(18, 34) if label else rng.uniform(0, 20)
            values["title_fit"] = rng.uniform(0.6, 1.0) if label else rng.uniform(0.0, 0.7)
            # A weight that comes out negative: under spec 077 a blocker's
            # sign could refuse a model; under spec 081 no weight's sign can.
            values["years_gap"] = rng.uniform(0, 1) if label else rng.uniform(0, 5)
        elif scenario == "rules_win":
            baseline = label * 100 + rng.uniform(0, 5)
        decided = start + timedelta(minutes=index * 37)
        rows.append(
            {
                "kind": "row",
                "job_id": index + 1,
                "decided_at": decided.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "live": rng.random() < live_share,
                "label": label,
                "features": [values[name] for name in FEATURE_ORDER],
                "baseline": baseline,
                "shown": str(round(baseline)),
                "shown_version": "0a1b2c3d4e5f",
            }
        )
    return rows


def _write_export(rows: list[dict[str, Any]], *, feature_order: list[str] | None = None) -> Path:
    header = {
        "kind": "header",
        "format_version": 1,
        "created_at": "2026-10-06",
        "feature_order": feature_order or list(FEATURE_ORDER),
        "rules_version": "5e5e5e5e5e5e",
        "rows": len(rows),
        "excluded": {UNDECIDED: 0},
    }
    path = exports_dir() / "features-20261006.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(header), *(json.dumps(row) for row in rows)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def quick_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fewer resamples keep the suite fast; the worlds are built so the
    verdict does not hang on the last digit of an interval."""
    monkeypatch.setattr(train_module, "BOOTSTRAP_RESAMPLES", 300)


def _report(path: Path | None) -> dict[str, Any]:
    assert path is not None
    return cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))


def test_the_split_is_time_ordered() -> None:
    rows = [
        Row(i, at, i % 2 == 0, i % 2, dict.fromkeys(FEATURE_ORDER, 0.0), 0.0, "", "")
        for i, at in enumerate(
            [
                "2026-09-03T10:00:00Z",
                "2026-03-02",
                "2026-08-15T08:00:00Z",
                "2026-04-11",
                "2026-10-01T09:30:00Z",
                "2026-08-15T07:59:59Z",
                "2026-05-20",
                "2026-09-30T23:00:00Z",
                "2026-06-06",
                "2026-07-07",
            ]
        )
    ]
    random.Random(3).shuffle(rows)
    training, testing = split(rows)
    assert len(training) == 7 and len(testing) == 3
    assert max(row.decided_at for row in training) <= min(row.decided_at for row in testing)


def test_p92_stats_come_from_training_rows_only(quick_bootstrap: None) -> None:
    rows = _synthetic_rows(800, seed=11, scenario="learnable")
    # Every later row carries an extreme count. Had the test rows informed the
    # scaling, the stored percentile would show it.
    for row in rows[560:]:
        row["features"][0] = 10_000.0
    _write_export(rows)
    outcome = train()
    assert outcome.model_path is not None
    stored = json.loads(outcome.model_path.read_text(encoding="utf-8"))["p92"]
    training, _ = split(read_export(exports_dir() / "features-20261006.jsonl")[1])
    assert stored == pytest.approx(p92_from(training))
    assert stored["skill_signal"] < 10_000.0


def test_the_old_score_is_never_a_feature() -> None:
    assert not FORBIDDEN_FEATURES & set(FEATURE_ORDER)
    rows = _synthetic_rows(10, seed=1, scenario="learnable")
    path = _write_export(rows, feature_order=[*FEATURE_ORDER[:-1], "fit_score"])
    with pytest.raises(ExportError, match="old score"):
        read_export(path)
    outcome = train()
    assert outcome.exit_code == 2
    assert any("old score" in message for message in outcome.messages)


def test_train_refuses_below_minimum_labels() -> None:
    _write_export(_synthetic_rows(120, seed=2, scenario="learnable"))
    outcome = train(activate=True)
    assert outcome.exit_code == 3
    assert any(message.startswith("insufficient labels") for message in outcome.messages)
    assert outcome.model_path is None
    assert not models_dir().exists() or not any(models_dir().iterdir())
    assert not active_model_path().exists()
    assert _report(outcome.report_path)["minimum"]["met"] is False


def test_train_refuses_a_model_that_does_not_beat_the_rules(quick_bootstrap: None) -> None:
    _write_export(_synthetic_rows(800, seed=4, scenario="rules_win"))
    outcome = train(activate=True)
    assert outcome.exit_code == 3
    assert any("does not beat the rules" in message for message in outcome.messages)
    assert outcome.model_path is not None and outcome.model_path.exists()
    assert not active_model_path().exists(), "a refused model was activated"
    assert _report(outcome.report_path)["ship"]["beats_rules"] is False


def test_train_ships_without_a_blocker_condition(quick_bootstrap: None) -> None:
    """Spec 081 replaced spec 077's blocker-weight refusal with a floor: a
    model that beats the rules ships whatever sign its weights take."""
    _write_export(_synthetic_rows(800, seed=5, scenario="learnable"))
    outcome = train(activate=True)
    report = _report(outcome.report_path)
    assert report["coefficients"]["years_gap"] < 0, "the world was meant to give one negative"
    assert outcome.exit_code == 0, outcome.messages
    assert outcome.activated
    assert set(report["ship"]) == {"beats_rules", "minimum_met"}
    assert not [message for message in outcome.messages if "blocker" in message]


def test_the_minimum_follows_the_feature_count() -> None:
    _write_export(_synthetic_rows(120, seed=9, scenario="learnable"))
    report = _report(train().report_path)
    assert report["minimum"]["train_positives"] == 10 * len(FEATURE_ORDER)


def test_a_model_that_earns_it_is_activated_and_scores(
    quick_bootstrap: None, cfg: dict[str, object]
) -> None:
    _write_export(_synthetic_rows(800, seed=6, scenario="learnable", live_share=0.5))
    outcome = train(activate=True)
    report = _report(outcome.report_path)
    assert outcome.exit_code == 0, outcome.messages
    assert outcome.activated and active_model_path().exists()
    assert report["ship"] == {"beats_rules": True, "minimum_met": True}
    assert "live_metrics" in report

    job = _posting("Senior Frontend Engineer", "Remote, Europe", f"Remote across Europe. {BODY}")
    assert fit_score_for(job, cfg).reasons[0].startswith("scorer=model:")


def test_live_only_drops_backfilled_events(quick_bootstrap: None) -> None:
    rows = _synthetic_rows(800, seed=8, scenario="learnable", live_share=0.6)
    live = sum(1 for row in rows if row["live"])
    _write_export(rows)

    everything = _report(train().report_path)
    assert everything["counts"]["rows"] == 800
    # Reported apart, so live decisions can be judged on their own.
    assert "live_metrics" in everything

    only_live = _report(train(live_only=True).report_path)
    assert only_live["counts"]["rows"] == live
    assert only_live["live_only"] is True


# --- the seam in the product ------------------------------------------------------------


def test_the_model_never_changes_a_gate_verdict(cfg: dict[str, object]) -> None:
    """The gates filter and the model ranks: with or without a model, the same
    postings pass and the same ones are rejected, for the same reasons."""
    postings = [
        _posting("Senior Frontend Engineer", "Remote, Europe", f"Remote, Europe. {BODY}"),
        _posting("Senior Frontend Engineer", "Hybrid, Berlin", f"Hybrid. {BODY}"),
        _posting("Senior Frontend Engineer", "Remote, United States", f"Remote. {BODY}"),
        _posting("Engineering Manager", "Remote, Europe", f"Remote, Europe. {BODY}"),
        _posting(
            "Frontend Engineer",
            "Remote, Europe",
            f"Anywhere in the US, W-2. Remote, Europe. {BODY}",
        ),
    ]
    for index, job in enumerate(postings):
        job["url"] = f"https://boards.example.com/quillfeather/gate-{index}"
        job["external_id"] = f"gate-{index}"

    def verdicts() -> tuple[list[str], dict[str, int]]:
        result = screen_jobs(
            [cast("NormalizedJob", dict(job)) for job in postings],
            candidate_cfg=cfg,
            hold_companies=set(),
            indexes=TrackerIndexes(),
            source_seen={},
            cache_descriptions=False,
        )
        return [str(row["url"]) for row in result.new_tracker_rows], dict(result.rejected_counts)

    without_model = verdicts()
    _activate()
    assert verdicts() == without_model


def test_every_scoring_path_is_never_in_git() -> None:
    raw = json.loads((ROOT / "config" / "data-classification.json").read_text(encoding="utf-8"))
    patterns = cast("list[str]", raw["never_in_git"]["patterns"])
    for path in (
        "data/scoring/active-model.json",
        "data/scoring/models/job-fit-20261006-0a1b2c3d4e5f.json",
        "data/scoring/exports/features-20261006.jsonl",
        "data/scoring/reports/report-20261006.json",
    ):
        assert any(fnmatch.fnmatch(path, pattern) for pattern in patterns), path
        ignored = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT, check=False)
        assert ignored.returncode == 0, f"{path} is not gitignored"


def test_the_scoring_commands_run(
    conn: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    job_id = _tracked(conn, 1)
    change_status(conn, str(job_id), "shortlist")
    assert main(["scoring", "export"]) == 0
    assert "exported 1 labelled jobs" in capsys.readouterr().out
    # One labelled job is far below the minimum: refused, the rules stay.
    assert main(["scoring", "train"]) == 3
    assert "insufficient labels" in capsys.readouterr().err
    assert not active_model_path().exists()
