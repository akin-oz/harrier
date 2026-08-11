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
from harrier.demo import is_demo_mode, resolve_config_path
from harrier.notify import build_telegram_message, send_telegram_message
from harrier.screening import (
    build_tracker_indexes,
    dedupe_normalized_jobs,
    screen_jobs,
)
from harrier.screening.config import load_candidate_config
from harrier.screening.descriptions import cache_job_descriptions
from harrier.screening.normalized import NormalizedJob
from harrier.screening.seen import load_seen, save_seen
from harrier.sources import fetch_many, scrub_secrets
from harrier.sources.apify_linkedin import (
    DEFAULT_COUNT as APIFY_DEFAULT_COUNT,
)
from harrier.sources.apify_linkedin import (
    fetch_apify_linkedin_jobs,
)
from harrier.sources.ashby import fetch_ashby_jobs
from harrier.sources.batch_exports import load_wellfound_exports, load_wttj_exports
from harrier.sources.greenhouse import fetch_greenhouse_jobs
from harrier.sources.lever import fetch_lever_jobs
from harrier.sources.remoteok import fetch_remoteok_jobs
from harrier.tracker import DuplicateJobError, add_job, list_jobs
from harrier.userconfig import (
    load_ats_feeds,
    load_discovery_settings,
    load_hold_companies,
    load_search_urls,
)

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
ProgressFn = Callable[[str, str], None]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _incoming_dir() -> Path:
    return data_dir() / "incoming"


# The most a single scheduled run may ask a paid actor for. An unbounded
# count is unbounded spend, and the value reaching this point may have come
# from an HTTP request rather than from the operator (spec 035).
APIFY_MAX_COUNT = 500


def scheduled_apify_count(
    config_path: Path | None = None, conn: sqlite3.Connection | None = None
) -> int:
    """The scheduled-run Apify count, from the store or config/discovery.json.

    Resolves the old repo's three-way discrepancy between 50, 150, and 200
    (docs/parity-matrix.md, section 8). The scheduled default is 50 because
    the old `scripts/run-all-intake.sh` passed `--apify-count 50`, with a
    comment calling it a per-search ceiling rather than a typical haul. 150
    stays the CLI default for a manual run.

    The numbers are values from the old repository's own scripts, not
    measurements of anyone's search, so they are design rationale rather than
    the observation class ADR-008 excludes. The phrase this replaced said
    what production "actually ran", which reads as an operational claim about
    a live install and is the reason it was queried on review (PR #39).

    Precedence is store, then file, then default, pinned by
    tests/test_discovery.py (test_scheduled_run_uses_configured_count for
    the store, test_scheduled_count_falls_back_to_the_file for the file,
    test_scheduled_count_defaults_when_nothing_is_configured for neither).
    config_path still overrides everything, for a caller that means one
    specific file."""
    if config_path is not None:
        settings = _settings_file(resolve_config_path(config_path))
    else:
        settings = load_discovery_settings(conn)
    raw = settings.get("apify_scheduled_count")
    if not isinstance(raw, int) or isinstance(raw, bool):
        return APIFY_DEFAULT_COUNT
    # Clamped where it is read, not only where it is written (spec 035).
    # This value is stored, so a count written before the validation existed,
    # or written directly into the database, is still executed by the next
    # scheduled run. Validating the write alone leaves that path open.
    return max(1, min(raw, APIFY_MAX_COUNT))


def _settings_file(path: Path) -> dict[str, object]:
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return cast("dict[str, object]", parsed) if isinstance(parsed, dict) else {}


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
    shadow: bool = False
    """Dual-run mode (spec 022): dry-run semantics plus no paid source.

    --dry-run alone still starts a billed Apify run and then discards the
    result, which is inherited from the old orchestrator. That is tolerable
    for a one-off preview and not for the cutover's dual-run period, which
    runs discovery on a schedule for a week.
    """

    def __post_init__(self) -> None:
        if self.shadow:
            self.dry_run = True


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
    hold_companies = load_hold_companies(conn)
    indexes = build_tracker_indexes(list_jobs(conn))
    source_seen = load_seen(source_name)
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
        save_seen(source_name, source_seen)

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
    feeds = load_ats_feeds(conn)

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
            logger.warning("RemoteOK import failed: %s", scrub_secrets(str(exc)))
            summaries.append(
                {
                    "source": "remoteok",
                    "fetched_count": 0,
                    "new_prospects": 0,
                    "errors": [scrub_secrets(str(exc))],
                }
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

    # What the actor was actually asked for, as opposed to what the caller
    # requested. On a scheduled run the count comes from the stored discovery
    # settings, so the two already differed, and the summary reported the one
    # that had no effect (spec 033). None means Apify did not run.
    apify_count_used: int | None = None

    paid_allowed = not is_demo_mode() and not options.shadow
    if _source_enabled("apify_linkedin", options.only_sources) and paid_allowed:
        # Apify is the one paid source and reaches the network outside the
        # fixture seam. The demo skips it rather than reporting a missing
        # token as an error a stranger would read as a broken clone, and a
        # shadow run skips it because it must be repeatable for free.
        apify_gate_open = not options.scheduled or apify_allowed_now(options.now)
        if apify_gate_open:
            count = scheduled_apify_count(conn=conn) if options.scheduled else options.apify_count
            apify_count_used = count
            report("apify_linkedin", "fetching")
            try:
                apify_jobs = fetch_apify_linkedin_jobs(
                    dataset_files=options.dataset_files,
                    count=count,
                    search_urls=load_search_urls(conn),
                )
            except Exception as exc:
                logger.warning("Apify LinkedIn import failed: %s", scrub_secrets(str(exc)))
                summaries.append(
                    {
                        "source": "apify_linkedin",
                        "fetched_count": 0,
                        "new_prospects": 0,
                        "errors": [scrub_secrets(str(exc))],
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
        # The count the actor was given, not the one the caller asked for.
        # These differ on every scheduled run, and after spec 035 they also
        # differ whenever an out-of-range value is clamped. A number in a
        # summary that does not describe the run it summarises is the same
        # defect as a score field nobody updated (spec 033).
        "apify_count": apify_count_used,
        "apify_count_requested": options.apify_count,
        "source_summaries": summaries,
    }

    # Dry runs have zero side effects: no writes and no notifications
    # (spec 011; stated change from the old accidental independence).
    if totals["new_prospects"] and options.notify and not options.dry_run:
        # send_telegram_message declines in demo mode, so no branch here.
        send_telegram_message(build_telegram_message(all_items))
    if not options.dry_run:
        target = _incoming_dir() / "job_imports_run.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(aggregate, indent=2, ensure_ascii=False), encoding="utf-8")

    return aggregate


def cast_items(items: list[object]) -> list[dict[str, object]]:
    return [cast("dict[str, object]", item) for item in items if isinstance(item, dict)]
