"""The learned score's model file, its inference, and the seam every scorer calls
(spec 077).

The model is logistic regression stored as JSON and evaluated here in plain
Python: no scikit-learn, no numpy and no pickle on this path. Unpickling
runs code chosen by whoever wrote the file, and a file under `data/` is one
bind mount away from anything else on the host; JSON is diffable, hashable,
and readable without the library that fitted it.

`fit_score_for` is the one place a job gets its score. When the model cannot
judge (no model, an invalid one, no description, an extractor error) the
rules score the job, and the row says which scorer produced it and why.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from harrier.db import data_dir
from harrier.scoring.features import (
    FEATURE_ORDER,
    NUMERIC_FEATURES,
    Extraction,
    extract,
    validate,
)
from harrier.screening.normalized import NormalizedJob
from harrier.screening.policy import policy_version
from harrier.screening.rules import CandidateConfig, blockers, score_job

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1
KIND = "logistic_regression"
# Binary for now: skipped or acted on. The format carries the classes so the
# ordinal model spec 077 defers can arrive by amendment without a new format.
CLASSES = (0, 1)
CLASS_VALUES = (0.0, 1.0)

# What a row's version says when the rules scored it.
NO_MODEL = "none"
IDENTITY_LENGTH = 12
TOP_CONTRIBUTIONS = 5
SIGNIFICANT_DIGITS = 12

MODEL_MISSING = "model-missing"
MODEL_INVALID = "model-invalid"
EXTRACTION_FAILED = "extraction-failed"

# Every score the model can give: `round(100 * p)` for p between 0 and 1.
MODEL_SCORE_BOUNDS = (0, 100)


def model_blocker_penalty() -> int:
    """The smallest penalty that puts any blocked posting below every
    unblocked model score (spec 081): spec 078's derivation, high minus low
    plus one, over the model's bounds instead of the rules'. Derived, never
    configured, so it moves if the bounds ever do."""
    low, high = MODEL_SCORE_BOUNDS
    return high - low + 1


def scoring_dir() -> Path:
    """Everything spec 077 writes: never-in-git, under `data/` (ADR-008)."""
    return data_dir() / "scoring"


def active_model_path() -> Path:
    return scoring_dir() / "active-model.json"


class ModelInvalidError(ValueError):
    """A model file the scorer refuses. The message is the check that failed."""


def model_identity(raw: bytes) -> str:
    """A model is its bytes: the first characters of their digest."""
    return hashlib.sha256(raw).hexdigest()[:IDENTITY_LENGTH]


def _sigmoid(z: float) -> float:
    # Split so neither branch overflows on a large margin.
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    exp_z = math.exp(z)
    return exp_z / (1.0 + exp_z)


def normalize_values(values: Mapping[str, float], p92: Mapping[str, float]) -> list[float]:
    """The vector the coefficients apply to, in `FEATURE_ORDER`: unbounded
    counts divided by their training 92nd percentile and clipped to 1, the
    rest as given. One function for the trainer and the scorer, so the two
    cannot normalize differently."""
    vector: list[float] = []
    for name in FEATURE_ORDER:
        value = float(values[name])
        if name in NUMERIC_FEATURES:
            # Clipped to 1 and no further (spec 077): a configured negative
            # weight can make a count negative, and that is information.
            value = min(1.0, value / p92[name])
        vector.append(value)
    return vector


@dataclass(frozen=True)
class Model:
    identity: str
    coefficients: tuple[float, ...]
    intercept: float
    p92: dict[str, float]

    def normalized(self, values: Mapping[str, float]) -> list[float]:
        return normalize_values(values, self.p92)

    def contributions(self, values: Mapping[str, float]) -> list[tuple[str, float]]:
        """Each feature's share of the margin, so the score can say why."""
        normalized = self.normalized(values)
        return [
            (name, coefficient * value)
            for name, coefficient, value in zip(
                FEATURE_ORDER, self.coefficients, normalized, strict=True
            )
        ]

    def probability(self, values: Mapping[str, float]) -> float:
        margin = self.intercept + sum(value for _, value in self.contributions(values))
        return _sigmoid(margin)


def _finite(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ModelInvalidError(f"{what} is not a number")
    try:
        number = float(value)
    except OverflowError as error:
        # An integer too large for a float is valid JSON and no weight.
        raise ModelInvalidError(f"{what} is not finite") from error
    if not math.isfinite(number):
        raise ModelInvalidError(f"{what} is not finite")
    return number


def parse_model(raw: bytes) -> Model:
    """Read a model file, refusing anything the extractor and scorer cannot
    honour. Every refusal names its check."""
    try:
        parsed: object = json.loads(raw)
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        # ValueError covers malformed JSON and an integer too long to convert;
        # a refusal of either is a refused model, never a crashed run.
        raise ModelInvalidError(f"not JSON: {type(error).__name__}") from error
    if not isinstance(parsed, dict):
        raise ModelInvalidError("not a JSON object")
    document = cast("dict[str, Any]", parsed)
    if document.get("format_version") != FORMAT_VERSION:
        raise ModelInvalidError(f"format_version is not {FORMAT_VERSION}")
    if document.get("kind") != KIND:
        raise ModelInvalidError(f"kind is not {KIND}")
    # A model trained against another extractor would apply its weights to
    # the wrong features. A new feature therefore means a retrain, never a
    # silent zero.
    if document.get("feature_order") != list(FEATURE_ORDER):
        raise ModelInvalidError("feature_order does not match the extractor")
    raw_coefficients = document.get("coefficients")
    if not isinstance(raw_coefficients, list):
        raise ModelInvalidError("coefficients is not a list")
    coefficient_list = cast("list[object]", raw_coefficients)
    if len(coefficient_list) != len(FEATURE_ORDER):
        raise ModelInvalidError("coefficients do not match feature_order")
    coefficients = tuple(
        _finite(value, f"coefficient {name}")
        for name, value in zip(FEATURE_ORDER, coefficient_list, strict=True)
    )
    intercept = _finite(document.get("intercept"), "intercept")
    if document.get("classes") != list(CLASSES) or document.get("class_values") != list(
        CLASS_VALUES
    ):
        raise ModelInvalidError("only the binary model is supported by format 1")
    if document.get("encoders") != {}:
        raise ModelInvalidError("encoders are not supported by format 1")
    raw_p92 = document.get("p92")
    if not isinstance(raw_p92, dict):
        raise ModelInvalidError("p92 is not an object")
    p92_document = cast("dict[str, object]", raw_p92)
    p92: dict[str, float] = {}
    for name in sorted(NUMERIC_FEATURES):
        if name not in p92_document:
            raise ModelInvalidError(f"p92 has no entry for {name}")
        value = _finite(p92_document[name], f"p92 {name}")
        if value <= 0:
            raise ModelInvalidError(f"p92 {name} is not positive")
        p92[name] = value
    return Model(
        identity=model_identity(raw),
        coefficients=coefficients,
        intercept=intercept,
        p92=p92,
    )


def _rounded(value: object) -> object:
    if isinstance(value, float):
        return float(f"{value:.{SIGNIFICANT_DIGITS}g}")
    if isinstance(value, dict):
        return {key: _rounded(item) for key, item in cast("dict[str, object]", value).items()}
    if isinstance(value, list | tuple):
        return [_rounded(item) for item in cast("Sequence[object]", value)]
    return value


def model_document(
    *,
    coefficients: Sequence[float],
    intercept: float,
    p92: Mapping[str, float],
    created_at: str,
    training: Mapping[str, object],
    evaluation: Mapping[str, object],
) -> dict[str, object]:
    """The file format, in one place for the writer and the reader."""
    return {
        "format_version": FORMAT_VERSION,
        "kind": KIND,
        "created_at": created_at,
        "feature_order": list(FEATURE_ORDER),
        "coefficients": [float(value) for value in coefficients],
        "intercept": float(intercept),
        "classes": list(CLASSES),
        "class_values": list(CLASS_VALUES),
        "p92": {name: float(p92[name]) for name in sorted(NUMERIC_FEATURES)},
        "encoders": {},
        "training": dict(training),
        "evaluation": dict(evaluation),
    }


def dump_model(document: Mapping[str, object]) -> bytes:
    """Sorted keys and fixed float precision, so the same fit is the same
    bytes and therefore the same identity."""
    text = json.dumps(_rounded(dict(document)), sort_keys=True, indent=2, ensure_ascii=True)
    return (text + "\n").encode("utf-8")


# --- the active model -----------------------------------------------------------

_cache: dict[str, tuple[tuple[int, int, int], Model | ModelInvalidError]] = {}
_warned: set[tuple[str, str, tuple[int, int, int] | None]] = set()


def _warn_once(reason: str, path: Path, detail: str, key: tuple[int, int, int] | None) -> None:
    """Once per process for each file state: a run that scores hundreds of
    postings says it once. The path and the failed check only; nothing about
    any posting or person.

    No model is the state of every installation until one is trained, so it
    is reported at info: a warning on every run would read as broken to
    anyone watching the log, which `tests/test_demo.py` holds a demo run to.
    A file that exists and is refused is a real fault, and warns.
    """
    marker = (reason, str(path), key)
    if marker in _warned:
        return
    _warned.add(marker)
    level = logging.INFO if reason == MODEL_MISSING else logging.WARNING
    logger.log(
        level,
        "learned score unavailable (%s: %s at %s); the rules score until there is one",
        reason,
        detail,
        path,
    )


def load_active_model() -> tuple[Model | None, str | None]:
    """The active model, or the fallback that applies when there is none."""
    path = active_model_path()
    try:
        stat = path.stat()
    except FileNotFoundError:
        _warn_once(MODEL_MISSING, path, "no model file", None)
        return None, MODEL_MISSING
    except OSError as error:
        _warn_once(MODEL_INVALID, path, f"unreadable ({error.strerror})", None)
        return None, MODEL_INVALID
    key = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
    cached = _cache.get(str(path))
    if cached is not None and cached[0] == key:
        result = cached[1]
    else:
        try:
            result = parse_model(path.read_bytes())
        except ModelInvalidError as error:
            result = error
        except OSError as error:
            # A read that failed is not a verdict on the file, so it is not
            # cached: the next call reads again. Cached, one transient error
            # kept a long-running API on the rules until the file changed.
            _warn_once(MODEL_INVALID, path, f"unreadable ({error.strerror})", None)
            return None, MODEL_INVALID
        _cache[str(path)] = (key, result)
    if isinstance(result, ModelInvalidError):
        _warn_once(MODEL_INVALID, path, str(result), key)
        return None, MODEL_INVALID
    return result, None


def active_model_identity() -> str:
    """What the policy version records: the model that would score, or none."""
    model, _ = load_active_model()
    return model.identity if model is not None else NO_MODEL


# --- the seam ---------------------------------------------------------------------


@dataclass(frozen=True)
class FitScore:
    """Everything a score writes, ready for `score_fields()`."""

    score: int
    reasons: list[str]
    version: str


def model_signals(
    model: Model,
    extraction: Extraction,
    probability: float,
    found: Sequence[tuple[str, str]] = (),
) -> list[str]:
    """Which scorer, how sure, and the largest reasons, signed.

    "Why this score" is always answerable from the row: the five largest
    contributions to the margin, each with its sign, then each blocker that
    floored the score with the phrase that fired it (spec 081), in the
    format the rule score uses (spec 078).
    """
    signals = [f"scorer=model:{model.identity}", f"p={probability:.2f}"]
    ranked = sorted(
        (item for item in model.contributions(extraction.values) if item[1] != 0.0),
        key=lambda item: (-abs(item[1]), item[0]),
    )
    for name, value in ranked[:TOP_CONTRIBUTIONS]:
        signals.append(f"{'+' if value > 0 else '-'}{name}({value:+.2f})")
    signals.extend(f'blocker={kind} "{phrase}"' for kind, phrase in found)
    signals.extend(extraction.notes)
    return signals


def _scored_by_rules(job: NormalizedJob, candidate_cfg: CandidateConfig, why: str) -> FitScore:
    score, reasons = score_job(job, candidate_cfg)
    return FitScore(
        score=score,
        reasons=["scorer=rules", f"fallback={why}", *reasons],
        version=policy_version(candidate_cfg, model=NO_MODEL),
    )


def fit_score_for(job: NormalizedJob, candidate_cfg: CandidateConfig) -> FitScore:
    """The score a job gets, from the model when it can judge and the rules
    when it cannot. A run never fails because the model did."""
    model, problem = load_active_model()
    if model is None:
        return _scored_by_rules(job, candidate_cfg, problem or MODEL_MISSING)
    missing = validate(job)
    if missing is not None:
        return _scored_by_rules(job, candidate_cfg, missing)
    try:
        extraction = extract(job, candidate_cfg)
    except Exception as error:
        # The type only: the message could quote the posting.
        logger.warning(
            "feature extraction failed (%s); the rules scored this posting",
            type(error).__name__,
        )
        return _scored_by_rules(job, candidate_cfg, EXTRACTION_FAILED)
    probability = model.probability(extraction.values)
    score = round(100 * probability)
    # A posting the candidate cannot take ranks below every posting they can,
    # under this scorer as under the rules (spec 081). Once, however many
    # blockers fire: it is already below everything eligible.
    found = blockers(job)
    if found:
        score -= model_blocker_penalty()
    return FitScore(
        score=score,
        reasons=model_signals(model, extraction, probability, found),
        version=policy_version(candidate_cfg, model=model.identity),
    )
