"""Fitting the learned score, offline, from the export alone (spec 077).

The one module that imports scikit-learn and numpy. An import-linter
contract keeps every other module, and therefore inference, free of both,
and the container image does not install them (`--no-dev`), which is why
`harrier scoring train` is a host-only command.

A model is written only when there is enough to learn from, and activated
only when it earns it: its ranking must beat the rule score on held-out rows
that came after everything it trained on. Everything else is reported and
refused, and the rules keep scoring. Blockers are not the model's to learn:
the scorer floors a blocked posting by rule (spec 081).
"""

# scikit-learn ships without type information; numpy's is complete. The
# unknowns below are scikit-learn's alone, confined to this module.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import numpy as np
import numpy.typing as npt
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.model_selection import TimeSeriesSplit

from harrier.atomicio import write_bytes_atomic, write_json_atomic
from harrier.scoring.export import EXPORT_FORMAT_VERSION, exports_dir
from harrier.scoring.features import FEATURE_ORDER, NUMERIC_FEATURES
from harrier.scoring.model import (
    active_model_path,
    dump_model,
    model_document,
    model_identity,
    normalize_values,
    scoring_dir,
)

FloatArray = npt.NDArray[np.float64]
IntArray = npt.NDArray[np.int64]

# The earliest share of decided jobs trains; the rest, all later, test.
TRAIN_SHARE = 0.7
# Minimum data (spec 077, Akin's judgement): ten positives per feature in the
# training window, the common rule of thumb below which logistic regression
# coefficients and their signs are unstable; and thirty in the test window,
# below which the bootstrap interval cannot tell a better ranker from a lucky
# one.
POSITIVES_PER_FEATURE = 10
MIN_TEST_POSITIVES = 30
# Regularization strengths tried by forward-chaining cross-validation inside
# the training window. A log grid: which one wins is the data's call.
C_GRID: tuple[float, ...] = (0.01, 0.1, 1.0, 10.0)
CV_SPLITS = 4
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 0
TOP_K = 10
# The old score and anything derived from it are never features: the queue
# ranked by them, so a model reading them would learn its own selection bias.
FORBIDDEN_FEATURES: frozenset[str] = frozenset({"fit_score", "score", "signals", "scoring_version"})

EXIT_OK = 0
EXIT_UNUSABLE = 2
EXIT_REFUSED = 3


class ExportError(ValueError):
    """An export the trainer will not read. The message says why."""


@dataclass(frozen=True)
class Row:
    job_id: int
    decided_at: str
    live: bool
    label: int
    features: dict[str, float]
    baseline: float
    shown: str
    shown_version: str


@dataclass
class TrainOutcome:
    exit_code: int
    messages: list[str] = field(default_factory=list[str])
    report_path: Path | None = None
    model_path: Path | None = None
    activated: bool = False


def reports_dir() -> Path:
    return scoring_dir() / "reports"


def models_dir() -> Path:
    return scoring_dir() / "models"


def latest_export() -> Path | None:
    exports = sorted(exports_dir().glob("features-*.jsonl"))
    return exports[-1] if exports else None


def read_export(path: Path) -> tuple[dict[str, Any], list[Row]]:
    """The export's header and rows, refusing one the extractor did not write."""
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        raise ExportError(f"{path.name} is empty")
    header = cast("dict[str, Any]", json.loads(lines[0]))
    if header.get("kind") != "header" or header.get("format_version") != EXPORT_FORMAT_VERSION:
        raise ExportError(f"{path.name} is not a format {EXPORT_FORMAT_VERSION} export")
    order = cast("list[str]", header.get("feature_order") or [])
    forbidden = sorted(FORBIDDEN_FEATURES & set(order))
    if forbidden:
        raise ExportError(
            f"the export names the old score as a feature ({', '.join(forbidden)}); "
            "the queue ranked by it, so a model would learn its own bias"
        )
    if order != list(FEATURE_ORDER):
        raise ExportError("the export's feature order does not match the extractor; export again")
    rows: list[Row] = []
    for line in lines[1:]:
        raw = cast("dict[str, Any]", json.loads(line))
        values = cast("list[float]", raw["features"])
        if len(values) != len(FEATURE_ORDER):
            raise ExportError("a row's features do not match the feature order")
        rows.append(
            Row(
                job_id=int(raw["job_id"]),
                decided_at=str(raw["decided_at"]),
                live=bool(raw["live"]),
                label=int(raw["label"]),
                features=dict(zip(FEATURE_ORDER, (float(v) for v in values), strict=True)),
                baseline=float(raw["baseline"]),
                shown=str(raw.get("shown", "")),
                shown_version=str(raw.get("shown_version", "")),
            )
        )
    return header, rows


def split(rows: Sequence[Row]) -> tuple[list[Row], list[Row]]:
    """The earliest decided jobs train and every later one tests, so the
    model is judged on decisions it could not have seen coming."""
    ordered = sorted(rows, key=lambda row: (row.decided_at, row.job_id))
    cut = math.floor(len(ordered) * TRAIN_SHARE)
    return ordered[:cut], ordered[cut:]


def p92_from(rows: Sequence[Row]) -> dict[str, float]:
    """Each unbounded feature's 92nd percentile over the given rows. Called
    with the training rows only, so test rows never inform their own
    scaling. A percentile of 0 is stored as 1: a feature that never fired in
    training then reads as itself rather than dividing by zero."""
    stats: dict[str, float] = {}
    for name in sorted(NUMERIC_FEATURES):
        values = [row.features[name] for row in rows]
        percentile = float(np.percentile(values, 92)) if values else 0.0
        stats[name] = percentile if percentile > 0 else 1.0
    return stats


def _matrix(rows: Sequence[Row], p92: dict[str, float]) -> FloatArray:
    return np.array([normalize_values(row.features, p92) for row in rows], dtype=np.float64)


def _labels(rows: Sequence[Row]) -> IntArray:
    return np.array([row.label for row in rows], dtype=np.int64)


def _average_precision(labels: IntArray, scores: FloatArray) -> float:
    return float(average_precision_score(labels, scores))


def _fit(features: FloatArray, labels: IntArray, strength: float) -> LogisticRegression:
    model = LogisticRegression(C=strength, max_iter=1000)
    model.fit(features, labels)
    return model


def _positive_probability(model: LogisticRegression, features: FloatArray) -> FloatArray:
    """The probability of "acted on" for each row, as a typed array."""
    probabilities = np.asarray(model.predict_proba(features), dtype=np.float64)
    return probabilities[:, 1]


def shipped_scores(probabilities: FloatArray) -> FloatArray:
    """The integer scores a model would write, as `fit_score_for` rounds them."""
    return np.round(100 * probabilities)


def choose_strength(features: FloatArray, labels: IntArray) -> float:
    """Forward-chaining cross-validation inside the training window: each fold
    trains on earlier rows and validates on the next ones, as the model will
    be used."""
    best_strength, best_score = 1.0, -1.0
    for strength in C_GRID:
        scores: list[float] = []
        for fit_index, check_index in TimeSeriesSplit(n_splits=CV_SPLITS).split(features):
            fit_labels, check_labels = labels[fit_index], labels[check_index]
            if len(set(fit_labels.tolist())) < 2 or check_labels.sum() == 0:
                continue
            fitted = _fit(features[fit_index], fit_labels, strength)
            predicted = _positive_probability(fitted, features[check_index])
            scores.append(_average_precision(check_labels, predicted))
        if scores and float(np.mean(scores)) > best_score:
            best_strength, best_score = strength, float(np.mean(scores))
    return best_strength


def _precision_at(labels: IntArray, scores: FloatArray, k: int) -> float:
    top = np.argsort(-scores, kind="stable")[:k]
    return float(labels[top].mean()) if len(top) else 0.0


def evaluate(labels: IntArray, model_scores: FloatArray, baseline: FloatArray) -> dict[str, Any]:
    """Model against rules on the same rows: average precision, with a paired
    bootstrap interval on the difference, and the measures that are reported
    but do not decide."""
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    differences: list[float] = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        index = rng.integers(0, len(labels), len(labels))
        sample = labels[index]
        if sample.sum() in (0, len(sample)):
            continue
        differences.append(
            _average_precision(sample, model_scores[index])
            - _average_precision(sample, baseline[index])
        )
    low, high = (
        (float(np.percentile(differences, 2.5)), float(np.percentile(differences, 97.5)))
        if differences
        else (0.0, 0.0)
    )
    both_classes = 0 < labels.sum() < len(labels)
    terciles: list[dict[str, Any]] = []
    cut_low, cut_high = np.quantile(baseline, [1 / 3, 2 / 3])
    for name, mask in (
        ("low", baseline <= cut_low),
        ("middle", (baseline > cut_low) & (baseline <= cut_high)),
        ("high", baseline > cut_high),
    ):
        band = labels[mask]
        terciles.append(
            {
                "band": name,
                "rows": int(mask.sum()),
                "base_rate": float(band.mean()) if len(band) else 0.0,
                "model_average_precision": _average_precision(band, model_scores[mask])
                if 0 < band.sum() < len(band)
                else None,
            }
        )
    return {
        "metric": "average_precision",
        "model": _average_precision(labels, model_scores),
        "baseline": _average_precision(labels, baseline),
        "delta_ci95": [low, high],
        "roc_auc": {
            "model": float(roc_auc_score(labels, model_scores)) if both_classes else None,
            "baseline": float(roc_auc_score(labels, baseline)) if both_classes else None,
        },
        f"precision_at_{TOP_K}": {
            "model": _precision_at(labels, model_scores, TOP_K),
            "baseline": _precision_at(labels, baseline, TOP_K),
        },
        "base_rate": float(labels.mean()) if len(labels) else 0.0,
        "baseline_terciles": terciles,
    }


def _band(score: float, cut_low: float, cut_high: float) -> str:
    """Which third of a score range a value falls in, cut by value."""
    if score <= cut_low:
        return "low"
    return "middle" if score <= cut_high else "high"


def selection_bias(rows: Sequence[Row]) -> list[dict[str, Any]]:
    """For live decisions: the acted-on rate in each third of the score the
    row carried when it was decided, per scoring version. How much the shown
    ranking drove the decisions, measured rather than corrected (spec 077)."""
    by_version: dict[str, list[tuple[float, int]]] = {}
    for row in rows:
        if not row.live:
            continue
        try:
            shown = float(row.shown)
        except ValueError:
            continue
        by_version.setdefault(row.shown_version or "unknown", []).append((shown, row.label))
    table: list[dict[str, Any]] = []
    for version, pairs in sorted(by_version.items()):
        # Cut by score value, as `evaluate` cuts its terciles. Cutting a
        # sorted list by position split tied scores by label, which put the
        # skips in the lower band and the acted-on rows in the higher one.
        scores = np.array([score for score, _ in pairs], dtype=np.float64)
        cut_low, cut_high = (float(cut) for cut in np.quantile(scores, [1 / 3, 2 / 3]))
        for band in ("low", "middle", "high"):
            part = [label for score, label in pairs if _band(score, cut_low, cut_high) == band]
            table.append(
                {
                    "version": version,
                    "band": band,
                    "rows": len(part),
                    "acted_on_rate": sum(part) / len(part) if part else None,
                }
            )
    return table


def _write_report(report: dict[str, Any], stamp: str) -> Path:
    path = reports_dir() / f"report-{stamp.replace('-', '')}.json"
    write_json_atomic(path, report)
    return path


def train(
    export: Path | None = None,
    *,
    activate: bool = False,
    live_only: bool = False,
    today: str | None = None,
) -> TrainOutcome:
    """Fit, judge and maybe activate a model from one export."""
    stamp = today or datetime.now(UTC).date().isoformat()
    path = export or latest_export()
    if path is None:
        return TrainOutcome(EXIT_UNUSABLE, ["no export found; run `harrier scoring export` first"])
    try:
        header, rows = read_export(path)
    except (ExportError, OSError, ValueError, KeyError) as error:
        return TrainOutcome(EXIT_UNUSABLE, [f"{path.name}: {error}"])
    if live_only:
        rows = [row for row in rows if row.live]

    training, testing = split(rows)
    train_positives = sum(row.label for row in training)
    test_positives = sum(row.label for row in testing)
    minimum = {
        "train_positives": POSITIVES_PER_FEATURE * len(FEATURE_ORDER),
        "test_positives": MIN_TEST_POSITIVES,
    }
    enough = (
        train_positives >= minimum["train_positives"]
        and test_positives >= minimum["test_positives"]
        and train_positives < len(training)
        and test_positives < len(testing)
    )
    report: dict[str, Any] = {
        "created_at": stamp,
        "export": path.name,
        "live_only": live_only,
        "counts": {
            "rows": len(rows),
            "train_rows": len(training),
            "test_rows": len(testing),
            "train_positives": train_positives,
            "test_positives": test_positives,
            "excluded": header.get("excluded", {}),
        },
        "minimum": {**minimum, "met": enough},
    }
    if not enough:
        report["verdict"] = "insufficient labels"
        report_path = _write_report(report, stamp)
        return TrainOutcome(
            EXIT_REFUSED,
            [
                "insufficient labels: "
                f"{train_positives} training positives (minimum {minimum['train_positives']}), "
                f"{test_positives} test positives (minimum {minimum['test_positives']}). "
                "No model was written; the rules keep scoring."
            ],
            report_path=report_path,
        )

    p92 = p92_from(training)
    train_features, train_labels = _matrix(training, p92), _labels(training)
    test_features, test_labels = _matrix(testing, p92), _labels(testing)
    strength = choose_strength(train_features, train_labels)
    fitted = _fit(train_features, train_labels, strength)
    coefficients = [
        float(value) for value in np.asarray(fitted.coef_, dtype=np.float64).reshape(-1)
    ]
    intercept = float(np.asarray(fitted.intercept_, dtype=np.float64).reshape(-1)[0])
    # Judged as it would ship: the queue ranks by the integer `fit_score`,
    # `round(100 * p)`, and ties it creates are part of the ranking the rules
    # are compared with.
    model_scores = shipped_scores(_positive_probability(fitted, test_features))
    baseline = np.array([row.baseline for row in testing], dtype=np.float64)
    metrics = evaluate(test_labels, model_scores, baseline)

    live_test = [index for index, row in enumerate(testing) if row.live]
    live_labels = test_labels[live_test] if live_test else np.array([], dtype=np.int64)
    if not live_only and int(live_labels.sum()) >= MIN_TEST_POSITIVES:
        report["live_metrics"] = evaluate(live_labels, model_scores[live_test], baseline[live_test])
    elif not live_only:
        report["live_metrics"] = {
            "skipped": f"fewer than {MIN_TEST_POSITIVES} live test positives",
        }

    by_name = dict(zip(FEATURE_ORDER, coefficients, strict=True))
    beats_rules = metrics["delta_ci95"][0] > 0
    refusals: list[str] = []
    if not beats_rules:
        refusals.append(
            "does not beat the rules: the 95 percent interval of the average precision "
            "difference reaches zero"
        )

    document = model_document(
        coefficients=coefficients,
        intercept=intercept,
        p92=p92,
        created_at=stamp,
        training={
            "rows": len(training),
            "positives": train_positives,
            "split_at": testing[0].decided_at[:10],
        },
        evaluation={
            "metric": "average_precision",
            "model": metrics["model"],
            "baseline": metrics["baseline"],
            "delta_ci95": metrics["delta_ci95"],
        },
    )
    data = dump_model(document)
    identity = model_identity(data)
    model_path = models_dir() / f"job-fit-{stamp.replace('-', '')}-{identity}.json"
    write_bytes_atomic(model_path, data)

    messages: list[str] = []
    activated = False
    if refusals:
        messages.extend(f"refused: {reason}" for reason in refusals)
        if activate:
            messages.append("--activate refused; the rules keep scoring")
    elif activate:
        write_bytes_atomic(active_model_path(), data)
        activated = True
        messages.append(f"activated model {identity}")
    else:
        messages.append(f"model {identity} passed; activate it with --activate")

    report.update(
        {
            "split_at": testing[0].decided_at[:10],
            "regularization_c": strength,
            "metrics": metrics,
            "coefficients": by_name,
            "intercept": intercept,
            "ship": {"beats_rules": beats_rules, "minimum_met": True},
            "selection_bias": selection_bias(rows),
            "model": {"path": model_path.name, "identity": identity},
            "activated": activated,
            "verdict": "refused" if refusals else ("activated" if activated else "passed"),
        }
    )
    report_path = _write_report(report, stamp)
    return TrainOutcome(
        EXIT_REFUSED if refusals else EXIT_OK,
        messages,
        report_path=report_path,
        model_path=model_path,
        activated=activated,
    )
