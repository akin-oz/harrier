"""Batch prospect evaluation with auditable auto-reject (spec 015 port of
evaluate_prospects.py).

Auto-reject happens only with the explicit apply flag; every committed
rejection appends an audit entry. A dry run mutates nothing and writes no
audit entries. Stated change: the driver runs in process against the
database instead of shelling out through subprocess.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from harrier.db import data_dir
from harrier.offers.evaluate import (
    EvaluationError,
    evaluate_offer,
    report_path_for,
)
from harrier.screening.descriptions import load_cached_description
from harrier.tracker import list_jobs, set_status

logger = logging.getLogger(__name__)

AUDIT_FILENAME = "audit.jsonl"


def evaluations_dir() -> Path:
    return data_dir() / "evaluations"


@dataclass
class BatchOptions:
    apply: bool = False
    threshold: float = 0.8
    limit: int = 0
    refresh: bool = False
    include_borderline: bool = False


@dataclass
class BatchSummary:
    processed: int = 0
    skipped_existing: int = 0
    errors: int = 0
    auto_rejected: int = 0
    would_reject: int = 0
    verdict_counts: dict[str, int] = field(default_factory=dict[str, int])
    lines: list[str] = field(default_factory=list[str])


def _append_audit(entry: dict[str, object]) -> None:
    directory = evaluations_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / AUDIT_FILENAME).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def evaluate_prospects(conn: sqlite3.Connection, options: BatchOptions) -> BatchSummary:
    summary = BatchSummary()
    prospects = [row for row in list_jobs(conn) if row.get("status") == "prospect"]
    if options.limit > 0:
        prospects = prospects[: options.limit]

    reject_verdicts = {"skip"}
    if options.include_borderline:
        reject_verdicts.add("borderline")

    for row in prospects:
        company = row.get("company", "")
        title = row.get("title", "")
        url = row.get("url", "")
        job_id = int(row.get("id", "0"))

        report_path = report_path_for(company, title)
        if report_path.exists() and not options.refresh:
            summary.skipped_existing += 1
            summary.lines.append(f"report exists, skipping: {company} | {title}")
            continue

        jd_text = load_cached_description(url)
        try:
            result = evaluate_offer(conn, company, title, url, jd_text)
        except EvaluationError as exc:
            summary.errors += 1
            summary.lines.append(f"error: {company} | {title}: {exc}")
            continue

        summary.processed += 1
        verdict = result.verdict
        summary.verdict_counts[verdict.verdict] = summary.verdict_counts.get(verdict.verdict, 0) + 1
        summary.lines.append(
            f"{company} | {title}: verdict={verdict.verdict} "
            f"confidence={verdict.confidence:.2f} {verdict.reason[:140]}"
        )

        if verdict.verdict in reject_verdicts and verdict.confidence >= options.threshold:
            reason = f"ai-evaluation: {verdict.reason or f'verdict={verdict.verdict}'}"
            if options.apply:
                set_status(conn, job_id, "rejected", rejection_reason=reason[:300])
                _append_audit(
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "job_id": job_id,
                        "url": url,
                        "company": company,
                        "title": title,
                        "verdict": verdict.verdict,
                        "confidence": verdict.confidence,
                        "threshold": options.threshold,
                        "reason": reason,
                    }
                )
                summary.auto_rejected += 1
                summary.lines.append(f"auto-rejected: {company} | {title}")
            else:
                summary.would_reject += 1
                summary.lines.append(f"[dry-run] would reject: {company} | {title}")

    return summary
