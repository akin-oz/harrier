"""The discovery orchestrator (spec 011 port of run-job-imports.py and the
old run_source_import glue).

One run over all sources in priority order; screening, tracker writes, seen
state, per-source and aggregate summaries, and exactly one Telegram message.
Sources fetch; screening decides; the tracker's single write path persists.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from harrier.db import data_dir
from harrier.notify import build_telegram_message, send_telegram_message
from harrier.screening import (
    build_tracker_indexes,
    dedupe_normalized_jobs,
    screen_jobs,
)
from harrier.screening.config import load_candidate_config, load_hold_companies
from harrier.screening.descriptions import cache_job_descriptions
from harrier.screening.normalized import NormalizedJob
from harrier.screening.seen import load_seen_keys, save_seen_keys
from harrier.sources import fetch_many
from harrier.sources.apify_linkedin import (
    DEFAULT_COUNT as APIFY_DEFAULT_COUNT,
)
from harrier.sources.apify_linkedin import (
    fetch_apify_linkedin_jobs,
)
from harrier.sources.ashby import fetch_ashby_jobs
from harrier.sources.batch_exports import load_wellfound_exports, load_wttj_exports
from harrier.sources.feeds import parse_ats_feeds
from harrier.sources.greenhouse import fetch_greenhouse_jobs
from harrier.sources.lever import fetch_lever_jobs
from harrier.sources.remoteok import fetch_remoteok_jobs
from harrier.tracker import DuplicateJobError, add_job, list_jobs

logger = logging.getLogger(__name__)

SOURCE_ORDER = (
    "greenhouse",
    "ashby",
    "lever",
    "remoteok",
    "apify_linkedin",
    "wellfound",
    "wttj",
)
DISCOVERY_CONFIG_PATH = Path("config") / "discovery.json"
ProgressFn = Callable[[str, str], None]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _incoming_dir() -> Path:
    return data_dir() / "incoming"


def scheduled_apify_count(config_path: Path | None = None) -> int:
    """The scheduled-run Apify count from config/discovery.json.

    Resolves the old repo's count discrepancy: 50 is what production ran
    (a per-search ceiling under the 24h search window; see the comment in
    config/discovery.json), 150 stays the CLI default."""
    path = config_path if config_path is not None else DISCOVERY_CONFIG_PATH
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return APIFY_DEFAULT_COUNT
    if isinstance(parsed, dict):
        raw: object = cast("dict[str, object]", parsed).get("apify_scheduled_count")
        if isinstance(raw, int):
            return raw
    return APIFY_DEFAULT_COUNT


def apify_allowed_now(now: datetime | None = None) -> bool:
    """Scheduled policy: Apify runs on weekday mornings only (cost gate)."""
    current = now if now is not None else datetime.now()
    return current.hour < 12 and current.isoweekday() <= 5


@dataclass
class DiscoveryOptions:
    dry_run: bool = False
    notify: bool = True
    only_sources: frozenset[str] = frozenset()
    apify_count: int = APIFY_DEFAULT_COUNT
    dataset_files: list[str] = field(default_factory=list[str])
    wellfound_files: list[str] = field(default_factory=list[str])
    wttj_files: list[str] = field(default_factory=list[str])
    scheduled: bool = False
    now: datetime | None = None


def _source_enabled(name: str, only_sources: frozenset[str]) -> bool:
    return not only_sources or name in only_sources


def _run_source(
    conn: sqlite3.Connection,
    source_name: str,
    jobs: list[NormalizedJob],
    fetched_count: int,
    *,
    dry_run: bool,
    extra: dict[str, object],
) -> dict[str, object]:
    """The old run_source_import, against the new store: screen, persist,
    save seen state, write the per-source summary. Dry runs mutate nothing."""
    candidate_cfg = load_candidate_config(conn)
    hold_companies = load_hold_companies()
    indexes = build_tracker_indexes(list_jobs(conn))
    source_seen = load_seen_keys(source_name)
    normalized_jobs = dedupe_normalized_jobs(jobs)

    result = screen_jobs(
        normalized_jobs,
        candidate_cfg=candidate_cfg,
        hold_companies=hold_companies,
        indexes=indexes,
        source_seen=source_seen,
        cache_descriptions=not dry_run,
    )

    persisted = 0
    if not dry_run:
        for row in result.new_tracker_rows:
            try:
                add_job(conn, row)
                persisted += 1
            except DuplicateJobError:
                # The screen already deduped; a race with a concurrent add is
                # the only path here. Count it as a tracker duplicate.
                result.skipped_tracker_duplicate += 1
        save_seen_keys(source_name, source_seen)

    summary: dict[str, object] = {
        "generated_at": _now_iso(),
        "dry_run": dry_run,
        "source": source_name,
        "fetched_count": fetched_count,
        "normalized_count": len(normalized_jobs),
        "source_duplicates": max(0, fetched_count - len(normalized_jobs)),
        "rejected_counts": result.rejected_counts,
        "new_prospects": len(result.new_tracker_rows) if dry_run else persisted,
        "tracker_duplicates": result.skipped_tracker_duplicate,
        "skipped_seen": result.skipped_seen,
        "skipped_hold": result.skipped_hold,
        "skipped_rejected": result.skipped_rejected,
        "items": result.latest_items,
    }
    summary.update(extra)
    if not dry_run:
        target = _incoming_dir() / f"{source_name}_latest.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def run_discovery(
    conn: sqlite3.Connection,
    options: DiscoveryOptions,
    progress: ProgressFn | None = None,
) -> dict[str, object]:
    def report(source: str, stage: str) -> None:
        if progress is not None:
            progress(source, stage)

    summaries: list[dict[str, object]] = []
    feeds = parse_ats_feeds()

    ats_fetchers: dict[str, Callable[[str], list[NormalizedJob]]] = {
        "greenhouse": fetch_greenhouse_jobs,
        "ashby": fetch_ashby_jobs,
        "lever": fetch_lever_jobs,
    }
    for source_name, fetcher in ats_fetchers.items():
        if not _source_enabled(source_name, options.only_sources):
            continue
        board_urls = feeds[source_name]
        if not board_urls:
            continue
        report(source_name, "fetching")
        jobs, board_errors = fetch_many(board_urls, fetcher, source_name)
        summaries.append(
            _run_source(
                conn,
                source_name,
                jobs,
                len(jobs),
                dry_run=options.dry_run,
                extra={"board_urls": board_urls, "board_errors": board_errors},
            )
        )
        report(source_name, "done")

    if _source_enabled("remoteok", options.only_sources):
        report("remoteok", "fetching")
        try:
            remoteok_jobs = fetch_remoteok_jobs()
        except Exception as exc:
            logger.warning("RemoteOK import failed: %s", exc)
            summaries.append(
                {"source": "remoteok", "fetched_count": 0, "new_prospects": 0, "errors": [str(exc)]}
            )
        else:
            summaries.append(
                _run_source(
                    conn,
                    "remoteok",
                    remoteok_jobs,
                    len(remoteok_jobs),
                    dry_run=options.dry_run,
                    extra={},
                )
            )
        report("remoteok", "done")

    if _source_enabled("apify_linkedin", options.only_sources):
        apify_gate_open = not options.scheduled or apify_allowed_now(options.now)
        if apify_gate_open:
            count = scheduled_apify_count() if options.scheduled else options.apify_count
            report("apify_linkedin", "fetching")
            try:
                apify_jobs = fetch_apify_linkedin_jobs(
                    dataset_files=options.dataset_files, count=count
                )
            except Exception as exc:
                logger.warning("Apify LinkedIn import failed: %s", exc)
                summaries.append(
                    {
                        "source": "apify_linkedin",
                        "fetched_count": 0,
                        "new_prospects": 0,
                        "errors": [str(exc)],
                    }
                )
            else:
                if not options.dry_run:
                    cached = cache_job_descriptions(apify_jobs)
                    if cached:
                        logger.info("description cache populated: %d items", cached)
                summaries.append(
                    _run_source(
                        conn,
                        "apify_linkedin",
                        apify_jobs,
                        len(apify_jobs),
                        dry_run=options.dry_run,
                        extra={"count": count, "dataset_files": options.dataset_files},
                    )
                )
            report("apify_linkedin", "done")
        else:
            logger.info("Apify skipped by scheduled policy (weekday mornings only)")

    if _source_enabled("wellfound", options.only_sources) and options.wellfound_files:
        report("wellfound", "fetching")
        wellfound_jobs = load_wellfound_exports(options.wellfound_files)
        summaries.append(
            _run_source(
                conn,
                "wellfound",
                wellfound_jobs,
                len(wellfound_jobs),
                dry_run=options.dry_run,
                extra={"input_files": options.wellfound_files},
            )
        )
        report("wellfound", "done")

    if _source_enabled("wttj", options.only_sources) and options.wttj_files:
        report("wttj", "fetching")
        wttj_jobs = load_wttj_exports(options.wttj_files)
        summaries.append(
            _run_source(
                conn,
                "wttj",
                wttj_jobs,
                len(wttj_jobs),
                dry_run=options.dry_run,
                extra={"input_files": options.wttj_files},
            )
        )
        report("wttj", "done")

    totals: Counter[str] = Counter()
    all_items: list[dict[str, object]] = []
    rejected_counts: Counter[str] = Counter()
    for summary in summaries:
        for key in (
            "fetched_count",
            "normalized_count",
            "new_prospects",
            "tracker_duplicates",
            "source_duplicates",
        ):
            totals[key] += int(str(summary.get(key, 0) or 0))
        items = summary.get("items")
        if isinstance(items, list):
            all_items.extend(cast_items(cast("list[object]", items)))
        raw_rejects = summary.get("rejected_counts")
        if isinstance(raw_rejects, dict):
            for key, value in cast("dict[str, object]", raw_rejects).items():
                rejected_counts[str(key)] += int(str(value))

    aggregate: dict[str, object] = {
        "generated_at": _now_iso(),
        "dry_run": options.dry_run,
        "sources_run": [str(summary.get("source", "")) for summary in summaries],
        "fetched_count": totals["fetched_count"],
        "normalized_count": totals["normalized_count"],
        "source_duplicates": totals["source_duplicates"],
        "tracker_duplicates": totals["tracker_duplicates"],
        "new_prospects": totals["new_prospects"],
        "rejected_counts": dict(rejected_counts),
        "apify_count": options.apify_count,
        "source_summaries": summaries,
    }

    # Dry runs have zero side effects: no writes and no notifications
    # (spec 011; stated change from the old accidental independence).
    if totals["new_prospects"] and options.notify and not options.dry_run:
        send_telegram_message(build_telegram_message(all_items))
    if not options.dry_run:
        target = _incoming_dir() / "job_imports_run.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(aggregate, indent=2, ensure_ascii=False), encoding="utf-8")

    return aggregate


def cast_items(items: list[object]) -> list[dict[str, object]]:
    return [cast("dict[str, object]", item) for item in items if isinstance(item, dict)]
