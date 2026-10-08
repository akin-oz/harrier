"""Discovery on an academic track (spec 097).

One configured source, the track's search entry, the academic gates in the
one screening path, and records kept apart from the default track's: its
summary lives under `incoming/<track id>/`, its success under
`discovery:<track id>`, its seen state under `discovery/<track id>/`. It
loads no candidate configuration, no hold list, no feeds and no profile
document, and runs no industry source.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from harrier.academic.search import (
    SearchEntry,
    SearchError,
    compile_input,
    parse_entry,
    policy_fingerprint,
    refusal_before_run,
    worst_case_usd,
)
from harrier.atomicio import write_json_atomic
from harrier.db import data_dir
from harrier.demo import fixtures_dir, is_demo_mode
from harrier.notify import build_academic_telegram_message, send_telegram_message
from harrier.runoutcome import age_in_days, last_success, record_success
from harrier.screening.normalized import dedupe_normalized_jobs
from harrier.screening.pipeline import (
    AcademicGates,
    build_academic_indexes,
    screen_jobs,
)
from harrier.screening.policy import academic_policy_version
from harrier.screening.seen import load_seen, save_seen
from harrier.sources import apify_academic as source
from harrier.tracker import DuplicateJobError, add_job
from harrier.tracker.store import all_tracks_dedupe_rows
from harrier.tracks import (
    Scope,
    UnknownTrackError,
    list_tracks,
    resolve_scope,
    rules_for,
)
from harrier.userconfig.store import ACADEMIC_SEARCHES, ConfigError, get_config

logger = logging.getLogger(__name__)

# The newest saved datasets each track keeps; older ones are evicted by age.
SAVED_RUNS_CAP = 20
DEMO_FIXTURE = "academic-dataset.json"
TOP_DETAILS = 10


class AcademicDiscoveryError(RuntimeError):
    """A refusal before any request: exit 2, naming the track or field."""


def discovery_job(scope: Scope) -> str:
    """The run-outcome key for this track's discovery (spec 029, 097)."""
    return f"discovery:{scope.track.id}"


def load_searches(conn: sqlite3.Connection) -> dict[str, object]:
    """The stored row, or empty. No file fallback (ADR-009, decision 2)."""
    value = get_config(conn, ACADEMIC_SEARCHES)
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def entry_for(conn: sqlite3.Connection, scope: Scope) -> SearchEntry:
    if rules_for(scope.track.kind).screening != "academic":
        raise AcademicDiscoveryError(
            f"track {scope.track.slug} is not an academic track; its discovery is the default one"
        )
    try:
        searches = load_searches(conn)
    except ConfigError as exc:
        raise AcademicDiscoveryError(str(exc)) from exc
    raw = searches.get(scope.track.slug)
    if raw is None:
        raise AcademicDiscoveryError(f"no search is configured for track {scope.track.slug}")
    try:
        entry = parse_entry(scope.track.slug, raw)
    except SearchError as exc:
        raise AcademicDiscoveryError(str(exc)) from exc
    refusal = refusal_before_run(entry)
    if refusal is not None:
        raise AcademicDiscoveryError(f"academic_searches[{scope.track.slug!r}]: {refusal}")
    return entry


@dataclass
class AcademicOptions:
    dry_run: bool = False
    shadow: bool = False
    notify: bool = True
    dataset_files: list[str] = field(default_factory=list[str])
    from_run: str = ""
    now: datetime | None = None
    transport: source.Transport | None = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic


def shadow_plan(entry: SearchEntry, scope: Scope) -> dict[str, object]:
    """What a run would send and keep, with no request and no write."""
    fingerprint = policy_fingerprint(entry)
    gates: dict[str, object] = {
        "exclude": [
            {"terms": [term.text for term in rule.terms], "in": list(rule.fields)}
            for rule in entry.exclude
        ],
        "position": (
            {
                "terms": [term.text for term in entry.position.terms],
                "in": list(entry.position.fields),
            }
            if entry.position is not None
            else None
        ),
        "areas": [
            {"label": area.label, "terms": [term.text for term in area.terms]}
            for area in entry.areas
        ],
        "require_area_match": entry.require_area_match,
        "deadline": "passed when more than one day before the run's date",
    }
    return {
        "track": scope.track.slug,
        "compiled_input": compile_input(entry),
        "ceilings": {
            "max_results": entry.max_results,
            "max_charge_usd": entry.max_charge_usd,
            "worst_case_usd": worst_case_usd(entry),
            "clamped": list(entry.clamped),
        },
        "gates": gates,
        "policy_version": academic_policy_version(fingerprint),
    }


def _runs_dir(scope: Scope) -> Path:
    return data_dir() / "discovery" / str(scope.track.id) / source.SOURCE_NAME / "runs"


def save_dataset(scope: Scope, run_id: str, items: list[dict[str, Any]]) -> Path:
    """The raw dataset and run id, before normalization, so a later change to
    the field map can be replayed too. Newest kept, up to the cap."""
    directory = _runs_dir(scope)
    target = directory / f"{run_id}.json"
    write_json_atomic(
        target,
        {"run_id": run_id, "saved_at": datetime.now(UTC).isoformat(), "items": items},
    )
    saved = sorted(directory.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for old in saved[SAVED_RUNS_CAP:]:
        old.unlink(missing_ok=True)
    return target


def _incoming_dir(scope: Scope) -> Path:
    return data_dir() / "incoming" / str(scope.track.id)


def _term_hits(entry: SearchEntry, hits: dict[str, int]) -> tuple[dict[str, int], list[str]]:
    terms: list[str] = []
    for area in entry.areas:
        terms.extend(term.text for term in area.terms)
    if entry.position is not None:
        terms.extend(term.text for term in entry.position.terms)
    counts = {term: hits.get(term, 0) for term in dict.fromkeys(terms)}
    return counts, [term for term, count in counts.items() if count == 0]


def _gap(conn: sqlite3.Connection, scope: Scope, entry: SearchEntry, now: datetime) -> str:
    last = last_success(conn, discovery_job(scope))
    if last is None:
        return ""
    # Stored timestamps are aware; a naive run time is the host's local time.
    age = age_in_days(last, now=now if now.tzinfo else now.astimezone())
    if age is not None and age > entry.window_days:
        return (
            f"the last success was {age} days ago, longer than the {entry.window_days}-day "
            "window: postings published in between were never requested"
        )
    return ""


def run_academic_discovery(
    conn: sqlite3.Connection, scope: Scope, options: AcademicOptions
) -> dict[str, object]:
    """One discovery on an academic track. Raises AcademicDiscoveryError for
    a refusal before any request; a failed run is reported in the summary
    with `failed` set, and writes nothing."""
    entry = entry_for(conn, scope)
    if options.shadow:
        return {"shadow": True, **shadow_plan(entry, scope)}

    now = options.now if options.now is not None else datetime.now()
    run_date = now.date()
    fingerprint = policy_fingerprint(entry)
    policy = academic_policy_version(fingerprint)
    compiled = compile_input(entry)

    summary: dict[str, object] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "track": scope.track.slug,
        "dry_run": options.dry_run,
        "source": source.SOURCE_NAME,
        "policy_version": policy,
        "run_id": "",
        "result_cap": entry.max_results,
        "charge_ceiling_usd": entry.max_charge_usd,
        "clamped": list(entry.clamped),
        "gap": _gap(conn, scope, entry, now),
    }

    run: source.ActorRun | None = None
    try:
        if options.dataset_files:
            items = source.read_dataset_files(options.dataset_files)
            summary["replayed_files"] = list(options.dataset_files)
        elif is_demo_mode() and not options.from_run:
            # The demo replays the synthetic fixture: no request, no keys.
            items = source.read_dataset_files([str(fixtures_dir() / DEMO_FIXTURE)])
            summary["replayed_files"] = [DEMO_FIXTURE]
        elif options.from_run:
            run = source.read_existing_run(
                options.from_run, max_charge_usd=entry.max_charge_usd, transport=options.transport
            )
            items = run.items
        else:
            run = source.run_actor(
                compiled,
                max_results=entry.max_results,
                max_charge_usd=entry.max_charge_usd,
                transport=options.transport,
                sleep=options.sleep,
                clock=options.clock,
            )
            items = run.items
            if not options.dry_run:
                summary["saved_dataset"] = str(save_dataset(scope, run.run_id, items))
    except (source.AcademicSourceError, RuntimeError, OSError, ValueError) as exc:
        # Nothing written and seen state unchanged, so the next run judges
        # the same postings. The message names no input value.
        summary["failed"] = str(exc)
        logger.warning("%s: %s", source.LOG_LABEL, exc)
        if options.notify and not options.dry_run:
            send_telegram_message(
                build_academic_telegram_message(scope.track.label, [], fetched=0, failed=str(exc))
            )
        return summary

    if run is not None:
        summary["run_id"] = run.run_id
        summary["run_status"] = run.status
        summary["charged_events"] = run.charged_events
        summary["charged_usd"] = run.charged_usd
        summary["pricing_differs"] = run.pricing_differs
        summary["stopped_at_ceiling"] = run.stopped_at_ceiling
    summary["dataset_items"] = len(items)
    if len(items) >= entry.max_results:
        summary["cap_reached"] = (
            "the run reached max_results: results ranked below the cap, including new ones, "
            "were not fetched"
        )

    normalized = source.normalize_items(items)
    unique = dedupe_normalized_jobs(normalized.jobs)
    portal_counts = Counter(str(job["metadata"].get("portal", source.NOT_STATED)) for job in unique)
    position_counts = Counter(
        str(job["metadata"].get("position_type", source.NOT_STATED)) for job in unique
    )
    listed_empty = [portal for portal in entry.portals if portal_counts.get(portal, 0) == 0]

    track_id = scope.track.id
    source_seen = load_seen(source.SOURCE_NAME, track_id)
    slugs = {str(track.id): track.slug for track in list_tracks(conn)}
    indexes = build_academic_indexes(all_tracks_dedupe_rows(conn), slugs)
    result = screen_jobs(
        unique,
        source_seen=source_seen,
        write_rejected_debug=options.dry_run,
        academic=AcademicGates(
            entry=entry,
            run_date=run_date,
            policy=policy,
            track_slug=scope.track.slug,
            indexes=indexes,
        ),
    )
    academic = result.academic
    assert academic is not None

    persisted = 0
    if not options.dry_run:
        for row in result.new_tracker_rows:
            try:
                add_job(conn, row, scope=scope)
                persisted += 1
            except DuplicateJobError:
                result.skipped_tracker_duplicate += 1
        save_seen(source.SOURCE_NAME, source_seen, track_id)

    hits, unused = _term_hits(entry, academic.term_hits)
    details = sorted(academic.rejected_details.items(), key=lambda item: (-item[1], item[0]))
    summary.update(
        {
            "fetched_count": len(items),
            "skipped_missing_title_or_url": normalized.missing_title_or_url,
            "deadline_unreadable": normalized.deadline_unreadable,
            "without_posting_date": normalized.without_posting_date,
            "dataset_duplicates": len(normalized.jobs) - len(unique),
            "skipped_seen": result.skipped_seen,
            "rejected_counts": result.rejected_counts,
            "rejected_details": dict(details[:TOP_DETAILS]),
            "kept": len(result.new_tracker_rows) if options.dry_run else persisted,
            "kept_without_deadline": academic.kept_without_deadline,
            "tracker_duplicates": result.skipped_tracker_duplicate,
            "portal_counts": dict(portal_counts),
            "listed_portals_without_items": listed_empty,
            "position_type_counts": dict(position_counts),
            "term_hits": hits,
            "unused_terms": unused,
        }
    )
    if options.dry_run:
        summary["rejected_postings"] = [
            {"title": row["title"], "organisation": row["company"], "reason": row["reject_reason"]}
            for row in result.rejected_debug_rows
        ]
        return summary

    record_success(conn, discovery_job(scope))
    target = _incoming_dir(scope) / f"{source.SOURCE_NAME}_latest.json"
    write_json_atomic(target, summary)
    if options.notify:
        send_telegram_message(
            build_academic_telegram_message(
                scope.track.label, result.latest_items, fetched=len(items)
            )
        )
    return summary


@dataclass
class TrackReport:
    slug: str
    summary: dict[str, object] | None = None
    problem: str = ""


def run_configured_tracks(conn: sqlite3.Connection, options: AcademicOptions) -> list[TrackReport]:
    """One discovery per track named in the stored search, each in its own
    scope. Never the default track. An unknown, archived or industry slug is
    reported, and one track's failure never stops the next."""
    reports: list[TrackReport] = []
    try:
        searches = load_searches(conn)
    except ConfigError as exc:
        # An unreadable stored search is reported, not raised: the weekly
        # job prints it and exits 3, as for any track it cannot run
        # (review of PR #187).
        return [TrackReport(ACADEMIC_SEARCHES, problem=str(exc))]
    for slug in searches:
        try:
            scope = resolve_scope(conn, slug)
        except UnknownTrackError:
            reports.append(TrackReport(slug, problem=f"no track named {slug}"))
            continue
        if scope.track.archived:
            reports.append(TrackReport(slug, problem=f"track {slug} is archived"))
            continue
        if rules_for(scope.track.kind).screening != "academic":
            reports.append(TrackReport(slug, problem=f"track {slug} is not an academic track"))
            continue
        try:
            summary = run_academic_discovery(conn, scope, options)
        except AcademicDiscoveryError as exc:
            reports.append(TrackReport(slug, problem=str(exc)))
            continue
        reports.append(TrackReport(slug, summary=summary))
    return reports


def protected_seen_keys(conn: sqlite3.Connection, scope: Scope) -> frozenset[str]:
    """The seen keys of postings the operator rejected on this track.

    A seen key is derived from the posting's identity
    (`harrier.screening.normalized.make_normalized_job`): the source, the
    organisation, and the external id or else the URL. Both forms are
    computed for every rejected row, so a rule change never reopens a human
    decision (spec 031's refusal, on an academic track).
    """
    from harrier.screening.normalized import normalize, stable_key
    from harrier.tracker.store import extract_note_value, list_jobs

    keys: set[str] = set()
    for job in list_jobs(conn, scope):
        if job.get("status") != "rejected":
            continue
        board = normalize(job.get("company", "") or source.SOURCE_NAME)
        external = job.get("external_key", "") or extract_note_value(
            job.get("notes", ""), "external_key"
        )
        prefix = f"{source.SOURCE_NAME}:"
        if external.startswith(prefix):
            keys.add(stable_key(source.SOURCE_NAME, board, external[len(prefix) :]))
        url = (job.get("url", "") or "").strip()
        if url:
            keys.add(stable_key(source.SOURCE_NAME, board, url))
    return frozenset(keys)
