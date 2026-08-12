"""The tracker write path. Nothing else opens the database for writing.

Behavior ports from the old repo's scripts/jobs.py: status setting stamps
next_action defaults, and marking applied seeds the outreach block
(scripts/jobs.py:394 in the old repo).
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from datetime import date, timedelta

from harrier.tracker.invariants import all_breaches
from harrier.tracker.schema import (
    CONTACT_FIELDS,
    NEXT_ACTION_DEFAULTS,
    NOTE_KEYS,
    STATUSES,
    TRACKER_FIELDS,
)
from harrier.tracker.transitions import check_transition, fields_a_move_clears


class TrackerError(Exception):
    pass


class DuplicateJobError(TrackerError):
    pass


class UnknownStatusError(TrackerError):
    pass


class JobNotFoundError(TrackerError):
    pass


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


def add_job(conn: sqlite3.Connection, fields: Mapping[str, str]) -> int:
    """Insert one job. Raises DuplicateJobError on url/external_key/company+title match."""
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
        values["next_action"] = NEXT_ACTION_DEFAULTS[status]

    existing = find_duplicate(
        conn, values["url"], values["company"], values["title"], promoted["external_key"]
    )
    if existing is not None:
        raise DuplicateJobError(
            f"duplicate of job id {existing['id']} ({existing['company']}: {existing['title']})"
        )

    columns = [name for name in TRACKER_FIELDS] + list(NOTE_KEYS)
    row_values = [values[name] if name != "status" else status for name in TRACKER_FIELDS]
    row_values += [promoted[key] for key in NOTE_KEYS]
    placeholders = ", ".join("?" for _ in columns)
    try:
        with conn:
            cursor = conn.execute(
                f"INSERT INTO jobs ({', '.join(columns)}) VALUES ({placeholders})",
                row_values,
            )
    except sqlite3.IntegrityError as error:
        # A concurrent writer can insert between find_duplicate and this
        # INSERT; the unique indexes are the authority, so map their refusal
        # to the same domain error the pre-check raises.
        raise DuplicateJobError(f"duplicate detected by unique index: {error}") from error
    row_id = cursor.lastrowid
    assert row_id is not None
    return int(row_id)


def get_job(conn: sqlite3.Connection, job_id: int) -> dict[str, str]:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise JobNotFoundError(f"no job with id {job_id}")
    return _job_row_to_dict(row)


def list_jobs(
    conn: sqlite3.Connection,
    status: str | None = None,
    source: str | None = None,
) -> list[dict[str, str]]:
    clauses: list[str] = []
    params: list[str] = []
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if source is not None:
        clauses.append("source = ?")
        params.append(source)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"SELECT * FROM jobs {where} ORDER BY id", params).fetchall()
    return [_job_row_to_dict(row) for row in rows]


def set_status(
    conn: sqlite3.Connection,
    job_id: int,
    status: str,
    *,
    applied_date: str | None = None,
    rejection_reason: str | None = None,
) -> dict[str, str]:
    """The only status setter. Enforces the transition and stamps what it drags.

    Membership in STATUSES used to be the whole check, so a job could go from
    prospect straight to interviewing, and three illegal states were reachable
    through this path (spec 036). The permitted moves are derived in
    `harrier.tracker.transitions`, and what a move must clear comes from the
    same place, so a new status cannot gain a rule in one and not the other.
    """
    if status not in STATUSES:
        raise UnknownStatusError(f"unknown status {status!r}; legal: {', '.join(STATUSES)}")
    job = get_job(conn, job_id)
    check_transition(job["status"], status)

    updates: dict[str, str] = {"status": status}
    updates.update(fields_a_move_clears(job["status"], status))
    if status == "applied":
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
        updates["next_action"] = NEXT_ACTION_DEFAULTS[status]
        if rejection_reason:
            updates["rejection_reason"] = rejection_reason
    else:
        updates["next_action"] = NEXT_ACTION_DEFAULTS[status]

    assignments = ", ".join(f"{name} = ?" for name in updates)
    with conn:
        conn.execute(
            f"UPDATE jobs SET {assignments}, updated_at = datetime('now') WHERE id = ?",
            [*updates.values(), job_id],
        )
    return get_job(conn, job_id)


def update_fields(
    conn: sqlite3.Connection, job_id: int, fields: Mapping[str, str]
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
        return get_job(conn, job_id)
    current = get_job(conn, job_id)
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
            f"UPDATE jobs SET {assignments}, updated_at = datetime('now') WHERE id = ?",
            [*[str(v) for v in fields.values()], job_id],
        )
    return get_job(conn, job_id)


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
