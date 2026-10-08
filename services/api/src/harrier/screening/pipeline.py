"""The shared screening pipeline (spec 007 port of screen_jobs).

Gate order is load-bearing (enrichment cost, Apify billing) and pinned by
tests: seen-state, hold list, title rules, remote/EMEA policy, tracker
dedupe, description enrichment, scoring.

There is no cutoff. Spec 033 removed it: it could only ever fire against
LinkedIn results, whose score floor is one bonus lower than the ATS path's,
so it penalised a source for behaving correctly. The gates filter and the
score ranks. This docstring said "scoring with the hard cutoff" for three
specs after that landed, while a comment 200 lines below said the opposite.

Persistence is the caller's job: this module returns tracker-ready rows and
mutates only the in-memory dedupe sets; nothing here writes the tracker
(single write path, ADR-003).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from harrier.screening.archetypes import detect_archetype
from harrier.screening.descriptions import (
    enrich_job_description_for_scoring,
    save_description_cache,
)
from harrier.screening.linkedin import (
    VERDICT_NOT_REMOTE,
    VERDICT_UNKNOWN,
    page_workplace_verdict,
)
from harrier.screening.normalized import NormalizedJob, normalize
from harrier.screening.policy import policy_version
from harrier.screening.rules import (
    AcademicTerm,
    CandidateConfig,
    academic_match,
    fold_text,
    remote_region_allowed,
    title_allowed,
)
from harrier.screening.seen import ACCEPTED, REJECTED, SeenDecision, now_iso
from harrier.sources.apify_academic import COMPONENTS as ACADEMIC_COMPONENTS
from harrier.tracker.schema import NEXT_ACTION_DEFAULTS
from harrier.tracker.score import score_fields

if TYPE_CHECKING:
    from harrier.academic.search import SearchEntry

# A deadline more than this many days before the run's date has passed
# (spec 097, "The deadline").
DEADLINE_GRACE_DAYS = 1

# The slug recorded when the remote or region gate rejects. Named here rather
# than taken from the gate's message, which is prose meant for a human and
# would silently split one cause into two if reworded.
REMOTE_REGION_REASON = "remote_region"

# The slug recorded when the posting's own LinkedIn page carries a JobPosting
# declaration without the remote tag (spec 055).
LINKEDIN_PAGE_REASON = "linkedin_page"


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
    # LinkedIn jobs whose page gave no workplace verdict and passed on the
    # text gates alone (spec 055). The size of the fail-open hole, per run.
    linkedin_unverified: int = 0
    # Set by the academic gates only (spec 097).
    academic: AcademicResult | None = None


def build_tracker_row(
    job: NormalizedJob, score: int, reasons: list[str], scoring_version: str = ""
) -> dict[str, str]:
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
        **score_fields(score, reasons, scoring_version),
        "status": "prospect",
        "next_action": NEXT_ACTION_DEFAULTS["prospect"],
        "contacts_found": "0",
        "notes": "; ".join(notes_parts),
    }


# The reason recorded when a posting is skipped because its company is on
# hold. Named once because the seen check reads it back (spec 052).
HOLD_REASON = "hold"


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
    candidate_cfg: CandidateConfig | None = None,
    hold_companies: set[str] | None = None,
    indexes: TrackerIndexes | None = None,
    source_seen: dict[str, SeenDecision],
    write_rejected_debug: bool = False,
    cache_descriptions: bool = True,
    policy: str | None = None,
    linkedin_page_verifier: Callable[[str], str] | None = None,
    academic: AcademicGates | None = None,
) -> ScreenResult:
    """The one screening path. The scope's kind picks the gates (spec 097):
    `academic` set runs the academic kind's gates; otherwise the industry
    gates run exactly as before, and need the candidate configuration, the
    hold list and the dedupe indexes."""
    if academic is not None:
        return _screen_academic(
            jobs, academic, source_seen, write_rejected_debug=write_rejected_debug
        )
    if candidate_cfg is None or hold_companies is None or indexes is None:
        raise TypeError("the industry gates need candidate_cfg, hold_companies and indexes")
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
        company_norm = normalize(job["company"])
        seen = source_seen.get(job_key)
        # A posting rejected only because its company was on hold is judged
        # again once that hold is no longer active. Holds lapse on their own
        # date (spec 052), and the seen check runs before the hold check, so
        # without this a posting fetched during a hold stayed skipped after
        # it (review finding on PR #104). Every other recorded decision is
        # skipped as before; spec 031's reconsider covers rule changes.
        hold_lapsed = (
            seen is not None
            and seen.verdict == REJECTED
            and seen.reason == HOLD_REASON
            and company_norm not in hold_companies
        )
        if not job_key or (seen is not None and not hold_lapsed):
            result.skipped_seen += 1
            continue

        title_norm = normalize(job["title"])
        url_norm = normalize(job["url"])
        external_id = (job["external_id"] or job["external_job_id"]).strip()
        external_key = normalize(f"{job['source']}:{external_id}") if external_id else ""

        if company_norm in hold_companies:
            reject_reason = HOLD_REASON
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
            # A slug, not the prose the gate returns. Every other gate records
            # one, and a stored reason that is a sentence cannot be grouped:
            # rewording the message in rules.py would silently split one cause
            # into two (review finding on PR #33). The prose stays in
            # rejected_counts, which is pre-existing behaviour that spec 032
            # revisits along with the rules themselves.
            record(job_key, REJECTED, REMOTE_REGION_REASON)
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

        # The posting's own page outranks everything the actor delivered
        # (spec 055): the actor omits the workplace declaration in practice,
        # and text evidence over-matches. Verified after the duplicate check
        # so a fetch is never spent on a posting already in the tracker, and
        # only for LinkedIn jobs: the other sources have no LinkedIn page.
        if job["remote_signal"] == "linkedin_search":
            # cache_descriptions is the run's one cache-write switch:
            # _run_source sets it to `not dry_run`, and a dry run must leave
            # no file behind, verdict cache included (dry-run contract in
            # discovery.py; review finding on PR #64).
            def _default_verifier(url: str) -> str:
                return page_workplace_verdict(url, write_cache=cache_descriptions)

            verifier = (
                linkedin_page_verifier if linkedin_page_verifier is not None else _default_verifier
            )
            page_verdict = verifier(job["url"])
            if page_verdict == VERDICT_NOT_REMOTE:
                record(job_key, REJECTED, LINKEDIN_PAGE_REASON)
                result.rejected_counts[LINKEDIN_PAGE_REASON] = (
                    result.rejected_counts.get(LINKEDIN_PAGE_REASON, 0) + 1
                )
                result.skipped_rejected += 1
                if write_rejected_debug:
                    result.rejected_debug_rows.append(
                        _build_rejected_debug_row(job, LINKEDIN_PAGE_REASON)
                    )
                continue
            if page_verdict == VERDICT_UNKNOWN:
                result.linkedin_unverified += 1

        scored_job = enrich_job_description_for_scoring(job)
        # Cached as soon as it is fetched, before anything downstream can
        # skip the job, so an enrichment fetch is never repeated for the same
        # URL (PR #4 review finding). This used to say "before the cutoff";
        # there is no cutoff now (spec 033) and the invariant is about the
        # fetch, not about what the score then does.
        if cache_descriptions:
            scored_url = scored_job["url"].strip()
            scored_desc = scored_job["description"].strip()
            if scored_url and scored_desc:
                save_description_cache(scored_url, scored_desc)
        # The one scoring seam (spec 077): the learned score when it can
        # judge, the rules when it cannot, and the version of whichever did.
        # Imported here, not at the top: `harrier.scoring` reads the
        # screening tables, and importing it first loads this package, whose
        # `__init__` loads this module. A module-level import is a cycle.
        from harrier.scoring.model import fit_score_for

        fit = fit_score_for(scored_job, candidate_cfg)
        # No cutoff. It could not reject an ATS posting and rejected LinkedIn
        # ones for being region-filtered at query level; the derivation is in
        # rules.py (spec 033).

        record(job_key, ACCEPTED, "passed every gate")
        result.new_tracker_rows.append(
            build_tracker_row(scored_job, fit.score, fit.reasons, fit.version)
        )
        result.latest_items.append(
            _build_latest_item(scored_job, fit.score, fit.reasons, remote_reason)
        )
        if url_norm:
            indexes.urls.add(url_norm)
        if company_norm and title_norm:
            indexes.company_title.add((company_norm, title_norm))
        if external_key:
            indexes.external_keys.add(external_key)

    return result


# --- the academic gates (spec 097) ----------------------------------------------
#
# The same path, the gates of another kind. Order is pinned
# (tests/test_academic_discovery.py::test_the_academic_gate_order_is_pinned),
# because the recorded reason is whichever gate fails first: seen state,
# exclusions, position, area, passed deadline, dedupe against every track.
# Every rejection records `<gate>:<detail>`. No industry title hint, remote
# gate, hold list, enrichment fetch or score is applied.

ACADEMIC_GATE_ORDER: tuple[str, ...] = (
    "seen",
    "exclude",
    "position",
    "area",
    "deadline",
    "dedupe",
)
DEADLINE_PASSED = "deadline_passed"
POSITION_UNMATCHED = "position_unmatched"
AREA_UNMATCHED = "area_unmatched"


def _clean_note(value: str) -> str:
    """A note value carries no separator: `;` ends a note, `|` joins list
    items and `=` joins a key to its value (harrier.tracker.store)."""
    return re.sub(r"[;|=]", " ", value).strip()


def normalize_link(url: str) -> str:
    """An application link compared across portals: scheme and host
    lowercased, fragment and trailing slash dropped, query string kept,
    because some boards identify a posting only by it."""
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


@dataclass
class AcademicIndexes:
    """Every track's identities, each with the slug of the track holding it."""

    urls: dict[str, str] = field(default_factory=dict[str, str])
    external_keys: dict[str, str] = field(default_factory=dict[str, str])
    apply_links: dict[tuple[str, str], str] = field(default_factory=dict[tuple[str, str], str])
    company_title: dict[tuple[str, str], list[tuple[str, str]]] = field(
        default_factory=dict[tuple[str, str], list[tuple[str, str]]]
    )

    def add(
        self,
        *,
        url: str,
        external_key: str,
        apply_link: str,
        company: str,
        title: str,
        deadline: str,
        slug: str,
    ) -> None:
        if url:
            self.urls.setdefault(url, slug)
        if external_key:
            self.external_keys.setdefault(external_key, slug)
        if apply_link:
            self.apply_links.setdefault((apply_link, deadline), slug)
        if company and title:
            self.company_title.setdefault((company, title), []).append((deadline, slug))

    def duplicate_of(
        self,
        *,
        url: str,
        external_key: str,
        apply_link: str,
        company: str,
        title: str,
        deadline: str,
    ) -> tuple[str, str] | None:
        """The identity that matched and the holding track's slug, or None.

        An organisation-and-title match is a duplicate only when the two
        deadlines are equal or either is empty; with either empty the rule
        is exactly the industry one.
        """
        if url and url in self.urls:
            return "url", self.urls[url]
        if external_key and external_key in self.external_keys:
            return "external_id", self.external_keys[external_key]
        if apply_link and (apply_link, deadline) in self.apply_links:
            return "apply_url", self.apply_links[(apply_link, deadline)]
        for other_deadline, slug in self.company_title.get((company, title), []):
            if not deadline or not other_deadline or deadline == other_deadline:
                return "organisation_title", slug
        return None


def build_academic_indexes(rows: list[dict[str, str]], slugs: dict[str, str]) -> AcademicIndexes:
    """From `all_tracks_dedupe_rows`; `slugs` maps a track id to its slug."""
    from harrier.tracker.store import extract_note_value

    indexes = AcademicIndexes()
    for row in rows:
        external_key = row.get("external_key", "") or extract_note_value(
            row.get("notes", ""), "external_key"
        )
        apply_url = extract_note_value(row.get("notes", ""), "apply_url")
        indexes.add(
            url=normalize(row.get("url", "")),
            external_key=normalize(external_key),
            apply_link=normalize_link(apply_url) if apply_url else "",
            company=normalize(row.get("company", "")),
            title=normalize(row.get("title", "")),
            deadline=row.get("deadline", "") or "",
            slug=slugs.get(str(row.get("track_id", "")), str(row.get("track_id", ""))),
        )
    return indexes


@dataclass
class AcademicGates:
    """What the academic kind screens with: the parsed search entry, the
    run's date (taken once, when the run starts), the policy version its
    decisions carry, and the slug of the track the kept rows land in."""

    entry: SearchEntry
    run_date: date
    policy: str
    track_slug: str
    indexes: AcademicIndexes


@dataclass
class AcademicResult:
    """What the academic screen found, for the summary (spec 097)."""

    rejected_details: dict[str, int] = field(default_factory=dict[str, int])
    term_hits: dict[str, int] = field(default_factory=dict[str, int])
    kept_without_deadline: int = 0


def _job_text(job: NormalizedJob, name: str) -> str:
    if name == "title":
        return job["title"]
    if name == "description":
        return job["description"]
    if name == "organisation":
        return job["company"]
    value = job["metadata"].get(name, "")
    text = str(value or "")
    return "" if text == "not stated" else text


def _first_match(
    terms: tuple[AcademicTerm, ...], fields: tuple[str, ...], job: NormalizedJob
) -> tuple[AcademicTerm, str] | None:
    for name in fields:
        term = academic_match(list(terms), _job_text(job, name))
        if term is not None:
            return term, name
    return None


def deadline_has_passed(deadline: str, run_date: date) -> bool:
    """More than one day before the run's date. The day of grace covers the
    time-zone and closing-hour difference between harrier's host and the
    institution; a deadline on the run's date or the day before is kept."""
    if not deadline:
        return False
    try:
        closes = date.fromisoformat(deadline)
    except ValueError:
        return False
    return closes < run_date - timedelta(days=DEADLINE_GRACE_DAYS)


def _flags(job: NormalizedJob, entry: SearchEntry) -> list[str]:
    """Each flag with the field and evidence that fired it. Never a gate."""
    fired: list[str] = []
    for name, phrases in sorted(entry.flag_phrases.items()):
        terms = [AcademicTerm(phrase) for phrase in phrases]
        for field_name in ("title", "description"):
            term = academic_match(terms, _job_text(job, field_name))
            if term is not None:
                fired.append(f"{name}:{field_name}:{_clean_note(term.text)}")
                break
    funding = _job_text(job, "funding")
    if funding:
        for value in entry.funding_flag_values:
            if fold_text(value) == fold_text(funding):
                fired.append(f"funding:funding:{_clean_note(funding)}")
                break
    return fired


def build_academic_row(
    job: NormalizedJob,
    *,
    area_hit: tuple[str, AcademicTerm, str] | None,
    position_hit: tuple[AcademicTerm, str] | None,
    flags: list[str],
) -> dict[str, str]:
    """A kept academic row: the parts of the decision and the source's
    components as notes, and no score, signals or remote filter (spec 097).
    The next action is left for the store to fill from the kind."""
    matched = (
        f"{_clean_note(area_hit[0])}:{_clean_note(area_hit[1].text)}@{area_hit[2]}"
        if area_hit is not None
        else "none"
    )
    notes = [f"matched={matched}"]
    if position_hit is not None:
        notes.append(f"position={_clean_note(position_hit[0].text)}@{position_hit[1]}")
    notes.append(f"flags={'|'.join(flags)}" if flags else "flags=not stated")
    for name in ACADEMIC_COMPONENTS:
        value = str(job["metadata"].get(name, "") or "not stated")
        notes.append(f"{name}={_clean_note(value) or 'not stated'}")
    deadline_text = str(job["metadata"].get("deadline_text", "") or "")
    if deadline_text:
        notes.append(f"deadline_text={_clean_note(deadline_text)}")
    external_id = (job["external_id"] or job["external_job_id"]).strip()
    if external_id:
        notes.append(f"external_key={job['source']}:{_clean_note(external_id)}")
    notes.append(f"source_label={_clean_note(job['source_label'])}")
    return {
        "company": job["company"],
        "title": job["title"],
        "location": job["location"],
        "url": job["url"],
        "source": job["source"],
        "added_at": datetime.now(UTC).date().isoformat(),
        "status": "prospect",
        "deadline": job["deadline"],
        "notes": "; ".join(notes),
    }


def _screen_academic(
    jobs: list[NormalizedJob],
    gates: AcademicGates,
    source_seen: dict[str, SeenDecision],
    *,
    write_rejected_debug: bool,
) -> ScreenResult:
    result = ScreenResult()
    academic = AcademicResult()
    result.academic = academic
    entry = gates.entry
    area_fields = ("title", "description", "subject")

    def record(key: str, verdict: str, reason: str) -> None:
        source_seen[key] = SeenDecision(verdict, reason, gates.policy, now_iso())

    def reject(job: NormalizedJob, key: str, reason: str) -> None:
        record(key, REJECTED, reason)
        gate = reason.split(":", 1)[0]
        result.rejected_counts[gate] = result.rejected_counts.get(gate, 0) + 1
        academic.rejected_details[reason] = academic.rejected_details.get(reason, 0) + 1
        result.skipped_rejected += 1
        if write_rejected_debug:
            result.rejected_debug_rows.append(_build_rejected_debug_row(job, reason))

    for job in jobs:
        job_key = job["job_key"].strip()
        seen = source_seen.get(job_key)
        # A passed deadline is judged again on every run that returns it, as
        # a lapsed hold is (spec 052), so an extended call is picked up.
        if not job_key or (seen is not None and seen.reason != DEADLINE_PASSED):
            result.skipped_seen += 1
            continue

        excluded: str | None = None
        for rule in entry.exclude:
            hit = _first_match(rule.terms, rule.fields, job)
            if hit is not None:
                excluded = f"exclude:{_clean_note(hit[0].text)}@{hit[1]}"
                break
        if excluded is not None:
            reject(job, job_key, excluded)
            continue

        position_hit: tuple[AcademicTerm, str] | None = None
        if entry.position is not None:
            position_hit = _first_match(entry.position.terms, entry.position.fields, job)
            if position_hit is None:
                reject(job, job_key, POSITION_UNMATCHED)
                continue

        area_hit: tuple[str, AcademicTerm, str] | None = None
        for area in entry.areas:
            hit = _first_match(area.terms, area_fields, job)
            if hit is not None:
                area_hit = (area.label, hit[0], hit[1])
                break
        if area_hit is None and entry.require_area_match:
            reject(job, job_key, AREA_UNMATCHED)
            continue

        if deadline_has_passed(job["deadline"], gates.run_date):
            reject(job, job_key, DEADLINE_PASSED)
            continue

        url_norm = normalize(job["url"])
        external_id = (job["external_id"] or job["external_job_id"]).strip()
        external_key = normalize(f"{job['source']}:{external_id}") if external_id else ""
        apply_url = str(job["metadata"].get("apply_url", "") or "")
        apply_link = normalize_link(apply_url) if apply_url and apply_url != "not stated" else ""
        company_norm = normalize(job["company"])
        title_norm = normalize(job["title"])
        duplicate = gates.indexes.duplicate_of(
            url=url_norm,
            external_key=external_key,
            apply_link=apply_link,
            company=company_norm,
            title=title_norm,
            deadline=job["deadline"],
        )
        if duplicate is not None:
            reject(job, job_key, f"tracker_duplicate:{duplicate[0]}:{duplicate[1]}")
            result.skipped_tracker_duplicate += 1
            continue

        record(job_key, ACCEPTED, "passed every gate")
        flags = _flags(job, entry)
        result.new_tracker_rows.append(
            build_academic_row(job, area_hit=area_hit, position_hit=position_hit, flags=flags)
        )
        result.latest_items.append(
            {
                "company": job["company"],
                "title": job["title"],
                "url": job["url"],
                "deadline": job["deadline"],
                "source": job["source"],
            }
        )
        if not job["deadline"]:
            academic.kept_without_deadline += 1
        for hit_term in (
            area_hit[1] if area_hit else None,
            position_hit[0] if position_hit else None,
        ):
            if hit_term is not None:
                academic.term_hits[hit_term.text] = academic.term_hits.get(hit_term.text, 0) + 1
        gates.indexes.add(
            url=url_norm,
            external_key=external_key,
            apply_link=apply_link,
            company=company_norm,
            title=title_norm,
            deadline=job["deadline"],
            slug=gates.track_slug,
        )
    return result
