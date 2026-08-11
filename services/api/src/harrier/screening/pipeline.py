"""The shared screening pipeline (spec 007 port of screen_jobs).

Gate order is load-bearing (enrichment cost, Apify billing) and pinned by
tests: seen-state, hold list, title rules, remote/EMEA policy, tracker
dedupe, description enrichment, scoring with the hard cutoff.

Persistence is the caller's job: this module returns tracker-ready rows and
mutates only the in-memory dedupe sets; nothing here writes the tracker
(single write path, ADR-003).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from harrier.screening.archetypes import detect_archetype
from harrier.screening.descriptions import (
    enrich_job_description_for_scoring,
    save_description_cache,
)
from harrier.screening.normalized import NormalizedJob, normalize
from harrier.screening.policy import policy_version
from harrier.screening.rules import (
    SCORE_CUTOFF,
    CandidateConfig,
    remote_region_allowed,
    score_job,
    title_allowed,
)
from harrier.screening.seen import ACCEPTED, REJECTED, SeenDecision, now_iso
from harrier.tracker.schema import NEXT_ACTION_DEFAULTS


@dataclass
class TrackerIndexes:
    """Cross-source dedupe sets built from tracker rows."""

    urls: set[str] = field(default_factory=set[str])
    company_title: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])
    external_keys: set[str] = field(default_factory=set[str])


def build_tracker_indexes(rows: list[dict[str, str]]) -> TrackerIndexes:
    """Rows come from harrier.tracker.list_jobs; external_key is a real column
    there (spec 004 promoted it), with the notes fallback kept for parity."""
    from harrier.tracker.store import extract_note_value

    indexes = TrackerIndexes()
    for row in rows:
        url = normalize(row.get("url", ""))
        company = normalize(row.get("company", ""))
        title = normalize(row.get("title", ""))
        external_key = normalize(
            row.get("external_key", "") or extract_note_value(row.get("notes", ""), "external_key")
        )
        if url:
            indexes.urls.add(url)
        if company and title:
            indexes.company_title.add((company, title))
        if external_key:
            indexes.external_keys.add(external_key)
    return indexes


@dataclass
class ScreenResult:
    rejected_counts: dict[str, int] = field(default_factory=dict[str, int])
    rejected_debug_rows: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    new_tracker_rows: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    latest_items: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    skipped_seen: int = 0
    skipped_tracker_duplicate: int = 0
    skipped_hold: int = 0
    skipped_rejected: int = 0


def build_tracker_row(job: NormalizedJob, score: int, reasons: list[str]) -> dict[str, str]:
    """Tracker-ready fields. The notes key=value string is built exactly as
    the old repo did; harrier.tracker.add_job promotes the keys to columns."""
    added_at = datetime.now(UTC).date().isoformat()
    archetype = detect_archetype(job["title"], job["description"])
    notes_parts = [
        f"score={score}",
        f"archetype={archetype}",
        f"source_label={job['source_label']}",
        "remote_filter=pass",
    ]
    external_id = (job["external_id"] or job["external_job_id"]).strip()
    if external_id:
        notes_parts.append(f"external_key={job['source']}:{external_id}")
    if reasons:
        notes_parts.append("signals=" + "|".join(reasons))
    return {
        "company": job["company"],
        "title": job["title"],
        "location": job["location"],
        "url": job["url"],
        "source": job["source"],
        "added_at": added_at,
        "fit_score": str(score),
        "status": "prospect",
        "next_action": NEXT_ACTION_DEFAULTS["prospect"],
        "contacts_found": "0",
        "notes": "; ".join(notes_parts),
    }


def _build_rejected_debug_row(job: NormalizedJob, reject_reason: str) -> dict[str, str]:
    return {
        "source": job["source"],
        "company": job["company"],
        "title": job["title"],
        "location": job["location"],
        "url": job["url"],
        "reject_reason": reject_reason,
    }


def _build_latest_item(
    job: NormalizedJob, score: int, reasons: list[str], remote_reason: str
) -> dict[str, object]:
    return {
        "company": job["company"],
        "title": job["title"],
        "location": job["location"],
        "url": job["url"],
        "source": job["source"],
        "fit_score": score,
        "reason": remote_reason,
        "signals": reasons,
        "created_at": job["created_at"] or job["posted_at"],
        "external_id": job["external_id"] or job["external_job_id"],
    }


def screen_jobs(
    jobs: list[NormalizedJob],
    *,
    candidate_cfg: CandidateConfig,
    hold_companies: set[str],
    indexes: TrackerIndexes,
    source_seen: dict[str, SeenDecision],
    write_rejected_debug: bool = False,
    cache_descriptions: bool = True,
    policy: str | None = None,
) -> ScreenResult:
    result = ScreenResult()
    current_policy = policy if policy is not None else policy_version(candidate_cfg)

    def record(key: str, verdict: str, reason: str) -> None:
        """Recorded after the decision, never before it (spec 031).

        The old code added the key on sight, so a posting suppressed before
        any gate ran could never be judged later and no rule change could
        ever reach it.
        """
        source_seen[key] = SeenDecision(verdict, reason, current_policy, now_iso())

    for job in jobs:
        job_key = job["job_key"].strip()
        if not job_key or job_key in source_seen:
            result.skipped_seen += 1
            continue

        company_norm = normalize(job["company"])
        title_norm = normalize(job["title"])
        url_norm = normalize(job["url"])
        external_id = (job["external_id"] or job["external_job_id"]).strip()
        external_key = normalize(f"{job['source']}:{external_id}") if external_id else ""

        if company_norm in hold_companies:
            reject_reason = "hold"
            record(job_key, REJECTED, reject_reason)
            result.rejected_counts[reject_reason] = result.rejected_counts.get(reject_reason, 0) + 1
            result.skipped_hold += 1
            if write_rejected_debug:
                result.rejected_debug_rows.append(_build_rejected_debug_row(job, reject_reason))
            continue

        if not title_allowed(job["title"], candidate_cfg):
            reject_reason = "title"
            record(job_key, REJECTED, reject_reason)
            result.rejected_counts[reject_reason] = result.rejected_counts.get(reject_reason, 0) + 1
            result.skipped_rejected += 1
            if write_rejected_debug:
                result.rejected_debug_rows.append(_build_rejected_debug_row(job, reject_reason))
            continue

        remote_ok, remote_reason = remote_region_allowed(job, candidate_cfg)
        if not remote_ok:
            record(job_key, REJECTED, remote_reason)
            result.rejected_counts[remote_reason] = result.rejected_counts.get(remote_reason, 0) + 1
            result.skipped_rejected += 1
            if write_rejected_debug:
                result.rejected_debug_rows.append(_build_rejected_debug_row(job, remote_reason))
            continue

        if (
            url_norm in indexes.urls
            or (company_norm, title_norm) in indexes.company_title
            or (external_key and external_key in indexes.external_keys)
        ):
            reject_reason = "tracker_duplicate"
            record(job_key, REJECTED, reject_reason)
            result.rejected_counts[reject_reason] = result.rejected_counts.get(reject_reason, 0) + 1
            result.skipped_tracker_duplicate += 1
            if write_rejected_debug:
                result.rejected_debug_rows.append(_build_rejected_debug_row(job, reject_reason))
            continue

        scored_job = enrich_job_description_for_scoring(job)
        # Cache before the cutoff: an enrichment fetch must never be repeated
        # for a job that then scores low (PR #4 review finding).
        if cache_descriptions:
            scored_url = scored_job["url"].strip()
            scored_desc = scored_job["description"].strip()
            if scored_url and scored_desc:
                save_description_cache(scored_url, scored_desc)
        score, reasons = score_job(scored_job, candidate_cfg)
        if score < SCORE_CUTOFF:
            reject_reason = "low_score"
            record(job_key, REJECTED, reject_reason)
            result.rejected_counts[reject_reason] = result.rejected_counts.get(reject_reason, 0) + 1
            result.skipped_rejected += 1
            if write_rejected_debug:
                result.rejected_debug_rows.append(
                    _build_rejected_debug_row(scored_job, reject_reason)
                )
            continue

        record(job_key, ACCEPTED, "passed every gate")
        result.new_tracker_rows.append(build_tracker_row(scored_job, score, reasons))
        result.latest_items.append(_build_latest_item(scored_job, score, reasons, remote_reason))
        if url_norm:
            indexes.urls.add(url_norm)
        if company_norm and title_norm:
            indexes.company_title.add((company_norm, title_norm))
        if external_key:
            indexes.external_keys.add(external_key)

    return result
