"""The tracker write path. Nothing else opens the database for writing.

Behavior ports from the old repo's scripts/jobs.py: status setting stamps
next_action defaults, and marking applied seeds the outreach block
(scripts/jobs.py:394 in the old repo).
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from collections import Counter
from collections.abc import Generator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import date, timedelta

from harrier.logredact import refresh_installed
from harrier.tracker.invariants import all_breaches
from harrier.tracker.reasons import (
    CANDIDATE,
    COMPANY,
    CREATED,
    DECISION,
    INTERVIEW_INVITED,
    OUTCOME,
    SYSTEM,
    ReasonError,
    classify_move,
    company_engaged,
)
from harrier.tracker.schema import (
    CONTACT_FIELDS,
    NOTE_KEYS,
    STATUSES,
    TRACKER_FIELDS,
)
from harrier.tracker.transitions import check_transition, fields_a_move_clears
from harrier.tracks import Scope, rules_for


class TrackerError(Exception):
    pass


class DuplicateJobError(TrackerError):
    pass


class UnknownStatusError(TrackerError):
    pass


class JobNotFoundError(TrackerError):
    pass


logger = logging.getLogger(__name__)


def extract_note_value(notes: str, key: str) -> str:
    """Port of the old repo's job_sources.extract_note_value, verbatim semantics."""
    match = re.search(rf"(?:^|;\s*){re.escape(key)}=([^;]+)(?:;|$)", notes or "")
    return match.group(1).strip() if match else ""


def expand_notes(notes: str) -> dict[str, str]:
    return {key: extract_note_value(notes, key) for key in NOTE_KEYS}


def _job_row_to_dict(row: sqlite3.Row) -> dict[str, str]:
    return {key: str(row[key]) for key in row.keys()}  # noqa: SIM118 - sqlite3.Row has no __iter__


def find_duplicate(
    conn: sqlite3.Connection, url: str, company: str, title: str, external_key: str
) -> dict[str, str] | None:
    """Dedupe order ports from the old screen path: url, external_key, company+title."""
    if url:
        row = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
        if row is not None:
            return _job_row_to_dict(row)
    if external_key:
        row = conn.execute("SELECT * FROM jobs WHERE external_key = ?", (external_key,)).fetchone()
        if row is not None:
            return _job_row_to_dict(row)
    if company and title:
        row = conn.execute(
            "SELECT * FROM jobs WHERE company = ? COLLATE NOCASE AND title = ? COLLATE NOCASE",
            (company, title),
        ).fetchone()
        if row is not None:
            return _job_row_to_dict(row)
    return None


def add_job(conn: sqlite3.Connection, fields: Mapping[str, str], *, scope: Scope) -> int:
    """Insert one job in the scope's track (spec 091).

    Raises DuplicateJobError on a url, external_key or company+title match.
    The track is the scope's to say: a `track_id` in `fields` is refused
    rather than honoured, so no caller can file a row somewhere its scope
    was not resolved for.
    """
    if "track_id" in fields:
        raise TrackerError("track_id is set from the scope, not from the fields")
    values = {name: str(fields.get(name, "") or "") for name in TRACKER_FIELDS}
    promoted = expand_notes(values["notes"])
    for key in NOTE_KEYS:
        override = str(fields.get(key, "") or "")
        if override:
            promoted[key] = override

    status = values["status"] or "prospect"
    if status not in STATUSES:
        raise UnknownStatusError(f"unknown status {status!r}; legal: {', '.join(STATUSES)}")
    if not values["next_action"]:
        values["next_action"] = rules_for(scope.track.kind).next_action[status]
    deadline = str(fields.get("deadline", "") or "").strip()
    if deadline and not _is_iso_date(deadline):
        raise TrackerError(f"deadline must be a YYYY-MM-DD date, got {deadline!r}")

    existing = find_duplicate(
        conn, values["url"], values["company"], values["title"], promoted["external_key"]
    )
    if existing is not None:
        raise DuplicateJobError(
            f"duplicate of job id {existing['id']} in track "
            f"{_track_slug(conn, existing['track_id'])} "
            f"({existing['company']}: {existing['title']})"
        )

    columns = [name for name in TRACKER_FIELDS] + list(NOTE_KEYS)
    row_values: list[object] = [
        values[name] if name != "status" else status for name in TRACKER_FIELDS
    ]
    row_values += [promoted[key] for key in NOTE_KEYS]
    row_values.append(scope.track.id)
    row_values.append(deadline)
    placeholders = ", ".join("?" for _ in columns)
    description_sha256 = _description_sha256(values["url"])
    with conn:
        try:
            cursor = conn.execute(
                f"INSERT INTO jobs ({', '.join(columns)}, track_id, deadline) "
                f"VALUES ({placeholders}, ?, ?)",
                row_values,
            )
        except sqlite3.IntegrityError as error:
            # A concurrent writer can insert between find_duplicate and this
            # INSERT; the unique indexes are the authority, so map their
            # refusal to the same domain error the pre-check raises.
            raise DuplicateJobError(f"duplicate detected by unique index: {error}") from error
        row_id = cursor.lastrowid
        assert row_id is not None
        # The row and its first event commit together (spec 079). A row a
        # person added by hand was created by the candidate; one discovery
        # found was created by the system.
        _append_event(
            conn,
            job_id=int(row_id),
            kind=CREATED,
            actor=CANDIDATE if promoted["manual_added"].strip() else SYSTEM,
            to_status=status,
            fit_score=values["fit_score"],
            scoring_version=promoted["scoring_version"],
            description_sha256=description_sha256,
        )
    return int(row_id)


def _is_iso_date(value: str) -> bool:
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _track_slug(conn: sqlite3.Connection, track_id: str) -> str:
    row = conn.execute("SELECT slug FROM tracks WHERE id = ?", (int(track_id),)).fetchone()
    return str(row[0]) if row is not None else f"id {track_id}"


def get_job(conn: sqlite3.Connection, scope: Scope, job_id: int) -> dict[str, str]:
    """One row of the scope's track. An id from another track is not found,
    with the same error a missing id raises and nothing about the other
    track's row in it (spec 092)."""
    row = conn.execute(
        "SELECT * FROM jobs WHERE id = ? AND track_id = ?", (job_id, scope.track.id)
    ).fetchone()
    if row is None:
        raise JobNotFoundError(f"no job with id {job_id}")
    return _job_row_to_dict(row)


def list_jobs(
    conn: sqlite3.Connection,
    scope: Scope,
    status: str | None = None,
    source: str | None = None,
) -> list[dict[str, str]]:
    """The scope's rows, in id order. Every reader of tracker rows reads one
    track (spec 092); the scope has no default so a reader cannot forget."""
    clauses: list[str] = []
    params: list[str | int] = [scope.track.id]
    if status is not None:
        clauses.append("AND status = ?")
        params.append(status)
    if source is not None:
        clauses.append("AND source = ?")
        params.append(source)
    rows = conn.execute(
        f"SELECT * FROM jobs WHERE track_id = ? {' '.join(clauses)} ORDER BY id", params
    ).fetchall()
    return [_job_row_to_dict(row) for row in rows]


def track_of_job(conn: sqlite3.Connection, job_id: int) -> int | None:
    """Which track a job id belongs to, or None when no job has it.

    For contact links only: a contact is the person's, not a track's, so a
    link can name a job in any track, and the checker has to tell a job in
    another track from one that is gone (review of PR #181). It returns the
    track and nothing of the row.
    """
    row = conn.execute("SELECT track_id FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return int(row[0]) if row is not None else None


def all_tracks_dedupe_rows(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """The identity columns of every row in every track, for the dedupe
    index only (spec 092). url and external_key are unique across the whole
    file, so a posting stored in one track is a duplicate in every other;
    this is the one reader that may see across tracks, and it returns the
    columns dedupe compares and nothing else.
    """
    rows = conn.execute(
        "SELECT url, external_key, company, title, notes, track_id FROM jobs ORDER BY id"
    ).fetchall()
    return [_job_row_to_dict(row) for row in rows]


@contextmanager
def _write_lock(conn: sqlite3.Connection) -> Generator[None, None, None]:
    """The write lock, taken before reading what a write depends on.

    A writer that read a row, decided, and only then opened its transaction
    let a second writer read the same row in between. On a job with no events
    both wrote its reconstructed history, permanently under the append-only
    trigger, and the later live event recorded a status the row no longer had
    (spec 079 amendment). BEGIN IMMEDIATE takes the lock at once, so a second
    writer waits for the first and reads what it wrote. A caller already
    inside a transaction keeps the lock it holds.
    """
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def company_has_responded(conn: sqlite3.Connection, scope: Scope, job_id: int) -> bool:
    """Whether a company outcome is recorded for this job (spec 079).

    The row forgets an invited interview once the company's first response
    closes it, and the events do not.
    """
    row = conn.execute(
        "SELECT 1 FROM job_events AS e JOIN jobs AS j ON j.id = e.job_id "
        "WHERE e.job_id = ? AND j.track_id = ? AND e.kind = 'outcome' AND e.actor = 'company' "
        "LIMIT 1",
        (job_id, scope.track.id),
    ).fetchone()
    return row is not None


def set_status(
    conn: sqlite3.Connection,
    scope: Scope,
    job_id: int,
    status: str,
    *,
    applied_date: str | None = None,
    rejection_reason: str | None = None,
    reason_code: str | None = None,
    actor: str | None = None,
    note: str | None = None,
) -> dict[str, str]:
    """The only status setter. Enforces the transition and stamps what it drags.

    Membership in STATUSES used to be the whole check, so a job could go from
    prospect straight to interviewing, and three illegal states were reachable
    through this path (spec 036). The permitted moves are derived in
    `harrier.tracker.transitions`, and what a move must clear comes from the
    same place, so a new status cannot gain a rule in one and not the other.

    Every move also appends its event, in the same transaction (spec 079),
    after the reconstructed history of a job that had none. Who made it and
    why is decided by `harrier.tracker.reasons.classify_move` from the row as
    it was, so a company's verdict is kept out of the candidate's decisions
    on every path that reaches here. `note` is free text for the event alone,
    for a move whose row has no field for it. The row is read under the
    write lock, so what the move is decided from is what it writes over.
    """
    if status not in STATUSES:
        raise UnknownStatusError(f"unknown status {status!r}; legal: {', '.join(STATUSES)}")
    with _write_lock(conn):
        job = get_job(conn, scope, job_id)
        check_transition(job["status"], status)
        try:
            move = classify_move(
                job,
                status,
                reason_code=reason_code,
                reason_text=rejection_reason if status == "rejected" else None,
                actor=actor,
                engaged=company_engaged(job) or company_has_responded(conn, scope, job_id),
            )
        except ReasonError as error:
            raise TrackerError(str(error)) from error
        if note is not None:
            reason_text = note
        else:
            reason_text = (rejection_reason or "") if status == "rejected" else ""
        description_sha256 = _description_sha256(job["url"])
        # A job decided before spec 079 has no events. Its first live event used
        # to make it look backfilled, so `harrier events backfill` skipped it and
        # its earlier history was lost for good under the append-only trigger
        # (spec 079 amendment). Its reconstruction now comes first, in
        # the same transaction, so a partial history cannot exist.
        history = [] if _has_events(conn, scope, job_id) else _plan_backfill(job)

        rules = rules_for(scope.track.kind)
        updates: dict[str, str] = {"status": status}
        updates.update(fields_a_move_clears(job["status"], status))
        if status == "applied" and not rules.seeds_follow_up:
            # A kind with no follow-up cadence and no outreach records the
            # date it was submitted and the kind's next action, and seeds
            # nothing else (spec 093).
            updates["applied_date"] = applied_date or date.today().isoformat()
            updates["next_action"] = rules.next_action[status]
        elif status == "applied":
            applied = applied_date or date.today().isoformat()
            follow_up = (date.fromisoformat(applied) + timedelta(days=7)).isoformat()
            updates["applied_date"] = applied
            updates["last_contact"] = applied
            updates["next_action"] = f"follow up if no reply by {follow_up}"
            # Seed the outreach block, filling only blanks (old repo: command_applied).
            updates["outreach_status"] = job["outreach_status"].strip() or "needs_contacts"
            updates["next_outreach_action"] = job["next_outreach_action"].strip() or "find contacts"
            updates["contacts_found"] = job["contacts_found"].strip() or "0"
            updates["outreach_priority"] = job["outreach_priority"].strip() or "high"
        elif status == "rejected":
            updates["next_action"] = rules.next_action[status]
            if rejection_reason:
                updates["rejection_reason"] = rejection_reason
        else:
            updates["next_action"] = rules.next_action[status]

        assignments = ", ".join(f"{name} = ?" for name in updates)
        with conn:
            for event in history:
                _append_event(
                    conn,
                    job_id=job_id,
                    kind=event.kind,
                    actor=event.actor,
                    from_status=event.from_status,
                    to_status=event.to_status,
                    reason_code=event.code,
                    reason_text=event.text,
                    at=event.at,
                    backfilled=True,
                )
            conn.execute(
                f"UPDATE jobs SET {assignments}, updated_at = datetime('now') "
                "WHERE id = ? AND track_id = ?",
                [*updates.values(), job_id, scope.track.id],
            )
            # What the candidate was looking at when they decided: the score and
            # version from before this write, which a later rescore overwrites on
            # the row but never here.
            _append_event(
                conn,
                job_id=job_id,
                kind=move.kind,
                actor=move.actor,
                from_status=job["status"],
                to_status=status,
                reason_code=move.code,
                reason_text=reason_text,
                fit_score=job["fit_score"],
                scoring_version=job.get("scoring_version", ""),
                description_sha256=description_sha256,
            )
    return get_job(conn, scope, job_id)


def update_fields(
    conn: sqlite3.Connection, scope: Scope, job_id: int, fields: Mapping[str, str]
) -> dict[str, str]:
    """Update non-status columns, refusing writes that break a status invariant.

    Blocking the status column here was not enough. `applied_date` could be
    cleared independently, leaving `status=applied` with no date, which is one
    of the illegal states spec 036 exists to close. The invariant belongs to
    the row rather than to the verb that happened to write it, so it is
    checked on every path into the row and not only on the status move.
    """
    allowed = set(TRACKER_FIELDS) | set(NOTE_KEYS)
    allowed.discard("status")
    unknown = [name for name in fields if name not in allowed]
    if unknown:
        raise TrackerError(
            f"fields not updatable here: {', '.join(sorted(unknown))} "
            f"(status changes go through set_status)"
        )
    if not fields:
        return get_job(conn, scope, job_id)
    # Read under the write lock, so the invariants are checked against the
    # row this write lands on (spec 079 amendment).
    with _write_lock(conn):
        current = get_job(conn, scope, job_id)
        # Only a breach this write introduces. Refusing every write to a row that
        # already breaks a rule would make rows written before these rules
        # unrepairable, and the spec is explicit that they are reported and left
        # alone rather than rewritten. `harrier check` is how they are found.
        before = set(all_breaches(current))
        after = all_breaches({**current, **{k: str(v) for k, v in fields.items()}})
        introduced = [breach for breach in after if breach not in before]
        if introduced:
            raise TrackerError(introduced[0])
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with conn:
            conn.execute(
                f"UPDATE jobs SET {assignments}, updated_at = datetime('now') "
                "WHERE id = ? AND track_id = ?",
                [*[str(v) for v in fields.values()], job_id, scope.track.id],
            )
    return get_job(conn, scope, job_id)


# --- decision history (spec 079) ----------------------------------------------


def _description_sha256(url: str) -> str:
    """The digest of the description cached for a URL, or empty when none is.

    It pins the text a decision was made on without copying it: the cache is
    keyed by URL and can be rewritten, and the event should still say which
    text was judged. Imported here rather than at module level because the
    screening package imports the tracker.
    """
    if not url:
        return ""
    from harrier.screening.descriptions import load_cached_description

    text = load_cached_description(url)
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""


def _append_event(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    kind: str,
    actor: str,
    to_status: str,
    from_status: str = "",
    reason_code: str = "",
    reason_text: str = "",
    fit_score: str = "",
    scoring_version: str = "",
    description_sha256: str = "",
    at: str | None = None,
    backfilled: bool = False,
) -> None:
    """The only INSERT into `job_events`. Runs inside the caller's transaction,
    so the event and the change it records commit together or not at all.

    The log line carries ids and codes only: the reason text is the
    candidate's own words and the row's company and title identify them.
    """
    fields: dict[str, object] = {
        "job_id": job_id,
        "kind": kind,
        "actor": actor,
        "from_status": from_status,
        "to_status": to_status,
        "reason_code": reason_code,
        "reason_text": reason_text,
        "fit_score": fit_score,
        "scoring_version": scoring_version,
        "description_sha256": description_sha256,
        "backfilled": 1 if backfilled else 0,
    }
    if at is not None:
        fields["at"] = at
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(
        f"INSERT INTO job_events ({', '.join(fields)}) VALUES ({placeholders})",
        list(fields.values()),
    )
    logger.debug("job event: job=%s kind=%s actor=%s code=%s", job_id, kind, actor, reason_code)


def _has_events(conn: sqlite3.Connection, scope: Scope, job_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM job_events AS e JOIN jobs AS j ON j.id = e.job_id "
        "WHERE e.job_id = ? AND j.track_id = ? LIMIT 1",
        (job_id, scope.track.id),
    ).fetchone()
    return row is not None


def list_events(conn: sqlite3.Connection, scope: Scope, job_id: int) -> list[dict[str, str]]:
    """A job's events in the order they were recorded, for a job of the
    scope's track. Another track's job is not found, with the same error a
    missing id raises (spec 092)."""
    get_job(conn, scope, job_id)
    rows = conn.execute(
        "SELECT e.* FROM job_events AS e JOIN jobs AS j ON j.id = e.job_id "
        "WHERE e.job_id = ? AND j.track_id = ? ORDER BY e.id",
        (job_id, scope.track.id),
    ).fetchall()
    return [_job_row_to_dict(row) for row in rows]


@dataclass(frozen=True)
class _PlannedEvent:
    at: str
    kind: str
    actor: str
    from_status: str
    to_status: str
    code: str = ""
    text: str = ""


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _as_event_time(value: str) -> str:
    """A stored timestamp in the event's format. SQLite's `datetime('now')`
    writes `YYYY-MM-DD HH:MM:SS` in UTC; a bare date is taken as its midnight."""
    text = (value or "").strip()
    if _ISO_DATE.match(text):
        return f"{text}T00:00:00Z"
    if len(text) >= 19 and text[10] in " T":
        return f"{text[:10]}T{text[11:19]}Z"
    return ""


def _plan_backfill(job: Mapping[str, str]) -> list[_PlannedEvent]:
    """The events a row's current fields can still vouch for.

    Its arrival; its application, when dated; and how it reached its present
    status. Nothing the row cannot show is invented: no score at decision
    time, no description digest, and a decision time that is an upper bound
    where only `updated_at` survives.

    Arrival is `added_at` when that is an earlier day than `created_at`: rows
    imported from the old tracker were created on the day of the import, and
    `added_at` is when they actually arrived.
    """
    created = _as_event_time(job.get("created_at", ""))
    added = _as_event_time(job.get("added_at", ""))
    # By day, not by instant: `added_at` is a bare date, and on the day a row
    # really arrived `created_at` carries the time as well.
    start = added if added and (not created or added[:10] < created[:10]) else created
    actor = CANDIDATE if (job.get("manual_added") or "").strip() else SYSTEM
    planned = [_PlannedEvent(start, CREATED, actor, "", "prospect")]
    current = "prospect"
    at = start

    applied = _as_event_time(job.get("applied_date", ""))
    if applied:
        at = max(at, applied)
        planned.append(_PlannedEvent(at, DECISION, CANDIDATE, current, "applied"))
        current = "applied"

    status = job.get("status", "")
    last = max(at, _as_event_time(job.get("updated_at", "")) or at)
    if status == "rejected":
        reason = job.get("rejection_reason", "")
        move = classify_move(job, "rejected", reason_text=reason)
        planned.append(
            _PlannedEvent(last, move.kind, move.actor, current, "rejected", move.code, reason)
        )
    elif status == "interviewing":
        planned.append(
            _PlannedEvent(last, OUTCOME, COMPANY, current, "interviewing", INTERVIEW_INVITED)
        )
    elif status != current:
        planned.append(_PlannedEvent(last, DECISION, CANDIDATE, current, status))
    return planned


def backfill_events(
    conn: sqlite3.Connection, scope: Scope, *, dry_run: bool = False
) -> Counter[tuple[str, str, str]]:
    """Events for every job of the scope's track that has none, reconstructed
    and marked so. Per track, because the reconstruction rules are a kind's
    (spec 092).

    Idempotent: a job with any event is skipped, so a second run writes
    nothing. Returns the count per kind, actor and code, which `--dry-run`
    prints without writing.
    """
    # Selected under the write lock, so a live first move cannot write the
    # same history in between (spec 079 amendment). A dry run writes nothing.
    with nullcontext() if dry_run else _write_lock(conn):
        rows = conn.execute(
            "SELECT * FROM jobs WHERE track_id = ? "
            "AND id NOT IN (SELECT job_id FROM job_events) ORDER BY id",
            (scope.track.id,),
        ).fetchall()
        plans = [(int(row["id"]), _plan_backfill(_job_row_to_dict(row))) for row in rows]
        counts: Counter[tuple[str, str, str]] = Counter(
            (event.kind, event.actor, event.code) for _, events in plans for event in events
        )
        if dry_run:
            return counts
        with conn:
            for job_id, events in plans:
                for event in events:
                    _append_event(
                        conn,
                        job_id=job_id,
                        kind=event.kind,
                        actor=event.actor,
                        from_status=event.from_status,
                        to_status=event.to_status,
                        reason_code=event.code,
                        reason_text=event.text,
                        at=event.at,
                        backfilled=True,
                    )
    return counts


def add_contact(conn: sqlite3.Connection, fields: Mapping[str, str]) -> int:
    values = [str(fields.get(name, "") or "") for name in CONTACT_FIELDS]
    placeholders = ", ".join("?" for _ in CONTACT_FIELDS)
    with conn:
        cursor = conn.execute(
            f"INSERT INTO contacts ({', '.join(CONTACT_FIELDS)}) VALUES ({placeholders})",
            values,
        )
    row_id = cursor.lastrowid
    assert row_id is not None
    # A contact is identity data the moment it exists, and this process may run
    # for days. Refreshing here rather than per log record keeps the database
    # off the logging path (spec 045, review of PR #49).
    refresh_installed(conn)
    return int(row_id)


def list_contacts(conn: sqlite3.Connection) -> list[dict[str, str]]:
    rows = conn.execute("SELECT * FROM contacts ORDER BY id").fetchall()
    return [_job_row_to_dict(row) for row in rows]


def update_contact_fields(
    conn: sqlite3.Connection, contact_id: int, fields: Mapping[str, str]
) -> None:
    """Update contact columns by id (spec 016). Unknown fields are an error."""
    unknown = [name for name in fields if name not in CONTACT_FIELDS]
    if unknown:
        raise TrackerError(f"unknown contact fields: {', '.join(sorted(unknown))}")
    if not fields:
        return
    assignments = ", ".join(f"{name} = ?" for name in fields)
    with conn:
        conn.execute(
            f"UPDATE contacts SET {assignments} WHERE id = ?",
            [*[str(value) for value in fields.values()], contact_id],
        )


def delete_contact(conn: sqlite3.Connection, contact_id: int) -> bool:
    with conn:
        cursor = conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
    return cursor.rowcount > 0
