"""The training export: labels and features, without the text they came from
(spec 077).

`harrier scoring export` runs where the database lives (inside the container
while it runs, spec 074) and writes one JSON Lines file. The trainer reads
that file and nothing else, so the code that needs scikit-learn never opens
the tracker.

The file holds a job's id, its label and when it was decided, the feature
vector from the same extractor inference uses, and the rule score as the
baseline the model must beat. It holds no company, title, URL, description
or reason text: the trainer does not need them, and an export is a file
that travels. It is still derived from personal rows, so it lives under
`data/scoring/` and never in git (ADR-008).
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from harrier.atomicio import write_bytes_atomic
from harrier.scoring.features import FEATURE_ORDER, extract
from harrier.scoring.labels import BLOCKED, EXCLUSIONS, Labelled, label_job
from harrier.scoring.model import NO_MODEL, scoring_dir
from harrier.screening.config import load_candidate_config
from harrier.screening.descriptions import load_cached_description
from harrier.screening.normalized import make_normalized_job
from harrier.screening.policy import policy_version
from harrier.screening.rules import blockers, score_job
from harrier.tracker.store import list_events, list_jobs
from harrier.tracks import DEFAULT_TRACK_ID, Scope

EXPORT_FORMAT_VERSION = 1


def exports_dir() -> Path:
    return scoring_dir() / "exports"


@dataclass(frozen=True)
class ExportResult:
    path: Path
    rows: int
    positives: int
    excluded: dict[str, int]


def export_features(
    conn: sqlite3.Connection, scope: Scope, *, today: str | None = None
) -> ExportResult:
    """Label every decided job, extract its features, and write the export.

    Reads the tracker and the description cache; writes nothing to either.
    """
    candidate_cfg = load_candidate_config(conn)
    excluded: Counter[str] = Counter({name: 0 for name in EXCLUSIONS})
    rows: list[dict[str, object]] = []
    for job in list_jobs(conn, scope):
        description = load_cached_description(job["url"])
        outcome = label_job(job, list_events(conn, scope, int(job["id"])), description)
        if not isinstance(outcome, Labelled):
            excluded[outcome] += 1
            continue
        normalized = make_normalized_job(
            source=job["source"] or "manual",
            company=job["company"],
            title=job["title"],
            location=job["location"],
            url=job["url"],
            description=description,
        )
        # The floor ranks a blocked posting, not the model (spec 081), and
        # training on it would teach the other features to explain a
        # rejection the blocker caused.
        if blockers(normalized):
            excluded[BLOCKED] += 1
            continue
        # The baseline is the rule score as it would be now, on the same
        # input the features come from, so model and rules are compared on
        # identical postings. The stored score is never read: it was produced
        # under whatever rules applied then, and it is what the queue ranked
        # by, which is the bias the report measures rather than a feature.
        baseline, _ = score_job(normalized, candidate_cfg)
        rows.append(
            {
                "kind": "row",
                "job_id": outcome.job_id,
                "decided_at": outcome.decided_at,
                "live": outcome.live,
                "label": outcome.label,
                "features": extract(normalized, candidate_cfg).vector(),
                "baseline": baseline,
                "shown": outcome.shown,
                "shown_version": outcome.shown_version,
            }
        )

    stamp = today or datetime.now(UTC).date().isoformat()
    header: dict[str, object] = {
        "kind": "header",
        "format_version": EXPORT_FORMAT_VERSION,
        "created_at": stamp,
        "feature_order": list(FEATURE_ORDER),
        # The rules the baseline was scored under.
        "rules_version": policy_version(candidate_cfg, model=NO_MODEL),
        "rows": len(rows),
        "excluded": dict(sorted(excluded.items())),
        # Which search the labels came from, so an export can never train a
        # model for another track's kind (spec 092, review of PR #181).
        "track": scope.track.slug,
        "track_kind": scope.track.kind,
    }
    lines = [json.dumps(header, sort_keys=True)]
    lines.extend(json.dumps(row, sort_keys=True) for row in rows)
    # The default track keeps the path the trainer has always read; any other
    # track writes under its own slug, so a same-day export of one track can
    # never overwrite another's (review of PR #181).
    directory = (
        exports_dir() if scope.track.id == DEFAULT_TRACK_ID else exports_dir() / scope.track.slug
    )
    path = directory / f"features-{stamp.replace('-', '')}.jsonl"
    write_bytes_atomic(path, ("\n".join(lines) + "\n").encode("utf-8"))
    return ExportResult(
        path=path,
        rows=len(rows),
        positives=sum(1 for row in rows if row["label"] == 1),
        excluded=dict(sorted(excluded.items())),
    )
