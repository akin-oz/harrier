"""The outreach state machine over applied rows (spec 016 port of
outreach_lib.py's tracker half).

States: needs_contacts, ready, sent, follow_up_sent, replied, snoozed.
Derivations mutate a row dict; sync_tracker_outreach persists them for
every job through the tracker store. Queue actions address jobs by id
(stated change from csv row selectors).
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from harrier.outreach.contacts import (
    best_contact,
    contacts_for_job,
    normalize,
    update_contact_status,
)
from harrier.tracker import get_job, list_contacts, list_jobs, update_fields

DUE_OUTREACH_ACTIONS = {"find contacts", "send first outreach", "send follow-up"}

OUTREACH_ROW_FIELDS = (
    "outreach_status",
    "next_outreach_action",
    "next_action",
    "contacts_found",
    "best_contact_name",
    "best_contact_linkedin",
    "outreach_priority",
)


def today_iso() -> str:
    return date.today().isoformat()


def business_days_after(start_date: str, days: int) -> str:
    current = date.fromisoformat(start_date)
    remaining = days
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current.isoformat()


def compute_outreach_priority(row: dict[str, str], contact_rows: list[dict[str, str]]) -> str:
    if normalize(row.get("status", "")) != "applied":
        return ""
    if not contact_rows:
        return "high"
    if normalize(row.get("outreach_status", "")) == "replied":
        return "low"
    return "medium"


def refresh_primary_next_action_from_outreach(row: dict[str, str]) -> None:
    if normalize(row.get("status", "")) != "applied":
        return
    outreach_action = (row.get("next_outreach_action") or "").strip()
    if not outreach_action:
        return
    action = normalize(outreach_action)
    last_outreach_at = (row.get("last_outreach_at") or "").strip()
    if action == "find contacts":
        row["next_action"] = "find contacts for outreach"
    elif action == "send first outreach":
        row["next_action"] = "send first outreach"
    elif action == "wait until outreach window":
        row["next_action"] = "wait until outreach window"
    elif action == "send follow-up":
        row["next_action"] = (
            f"send outreach follow-up by {business_days_after(last_outreach_at, 4)}"
            if last_outreach_at
            else "send outreach follow-up"
        )
    elif action == "wait for reply":
        row["next_action"] = (
            f"wait for outreach reply until {business_days_after(last_outreach_at, 4)}"
            if last_outreach_at
            else "wait for outreach reply"
        )
    elif action.startswith("snoozed until "):
        row["next_action"] = outreach_action


def refresh_outreach_fields(row: dict[str, str], contact_rows: list[dict[str, str]]) -> None:
    """Derive every outreach field from the row's status and its contacts
    (spec 016 state machine)."""
    row["contacts_found"] = str(len(contact_rows))
    chosen = best_contact(contact_rows)
    row["best_contact_name"] = chosen.get("person_name", "") if chosen else ""
    row["best_contact_linkedin"] = chosen.get("linkedin_url", "") if chosen else ""
    row["outreach_priority"] = compute_outreach_priority(row, contact_rows)

    applied_date = (row.get("applied_date") or "").strip()
    outreach_status = normalize(row.get("outreach_status", ""))
    last_outreach_at = (row.get("last_outreach_at") or "").strip()

    if normalize(row.get("status", "")) != "applied":
        row["next_outreach_action"] = ""
        return
    if outreach_status == "replied":
        row["next_outreach_action"] = ""
        row["next_action"] = "continue conversation with contact"
        return
    if outreach_status == "snoozed":
        # A snoozed row stays snoozed through sync; it must never surface
        # as due (review finding: the no-contacts branch was overriding it).
        refresh_primary_next_action_from_outreach(row)
        return
    if not contact_rows:
        row["outreach_status"] = outreach_status or "needs_contacts"
        row["next_outreach_action"] = "find contacts"
        refresh_primary_next_action_from_outreach(row)
        return
    if outreach_status in {"", "needs_contacts"}:
        row["outreach_status"] = "ready"
        if applied_date and date.today() >= date.fromisoformat(
            business_days_after(applied_date, 3)
        ):
            row["next_outreach_action"] = "send first outreach"
        else:
            row["next_outreach_action"] = "wait until outreach window"
        refresh_primary_next_action_from_outreach(row)
        return
    if outreach_status == "sent" and last_outreach_at:
        if date.today() >= date.fromisoformat(business_days_after(last_outreach_at, 4)):
            row["next_outreach_action"] = "send follow-up"
        else:
            row["next_outreach_action"] = "wait for reply"
        refresh_primary_next_action_from_outreach(row)
        return
    if outreach_status == "follow_up_sent":
        row["next_outreach_action"] = "wait for reply"
        refresh_primary_next_action_from_outreach(row)


def _persist_outreach_fields(conn: sqlite3.Connection, row: dict[str, str]) -> None:
    update_fields(
        conn,
        int(row["id"]),
        {name: row.get(name, "") for name in OUTREACH_ROW_FIELDS},
    )


def sync_tracker_outreach(conn: sqlite3.Connection) -> list[dict[str, str]]:
    contacts = list_contacts(conn)
    rows = list_jobs(conn)
    for row in rows:
        refresh_outreach_fields(
            row,
            contacts_for_job(
                contacts, row.get("company", ""), row.get("title", ""), row.get("url", "")
            ),
        )
        _persist_outreach_fields(conn, row)
    return rows


def mark_job_outreach_sent(
    conn: sqlite3.Connection, job_id: int, *, sent_at: str | None = None
) -> dict[str, str]:
    row = get_job(conn, job_id)
    sent_at = sent_at or today_iso()
    current = normalize(row.get("outreach_status", ""))
    # Legal transitions only: ready (or fresh) -> sent, sent -> follow_up_sent
    # (review finding: replied/snoozed/follow_up_sent must not move backward).
    if current not in {"", "needs_contacts", "ready", "sent"}:
        raise ValueError(f"cannot mark outreach sent from state {current!r}")
    row["outreach_status"] = "follow_up_sent" if current == "sent" else "sent"
    row["last_outreach_at"] = sent_at
    row["next_outreach_action"] = "wait for reply"
    refresh_primary_next_action_from_outreach(row)
    update_fields(
        conn,
        job_id,
        {
            "outreach_status": row["outreach_status"],
            "last_outreach_at": row["last_outreach_at"],
            "next_outreach_action": row["next_outreach_action"],
            "next_action": row["next_action"],
        },
    )
    if row.get("best_contact_linkedin"):
        update_contact_status(
            conn, row["best_contact_linkedin"], contact_status="sent", contacted_at=sent_at
        )
    return row


def mark_job_outreach_replied(
    conn: sqlite3.Connection, job_id: int, *, replied_at: str | None = None
) -> dict[str, str]:
    row = get_job(conn, job_id)
    replied_at = replied_at or today_iso()
    row["outreach_status"] = "replied"
    row["last_outreach_at"] = replied_at
    row["next_outreach_action"] = ""
    row["next_action"] = "continue conversation with contact"
    update_fields(
        conn,
        job_id,
        {
            "outreach_status": "replied",
            "last_outreach_at": replied_at,
            "next_outreach_action": "",
            "next_action": "continue conversation with contact",
        },
    )
    if row.get("best_contact_linkedin"):
        update_contact_status(
            conn,
            row["best_contact_linkedin"],
            contact_status="replied",
            reply_status="replied",
            contacted_at=replied_at,
        )
    return row


def snooze_job_outreach(conn: sqlite3.Connection, job_id: int, until_date: str) -> dict[str, str]:
    date.fromisoformat(until_date)  # validate early
    updates = {
        "outreach_status": "snoozed",
        "next_outreach_action": f"snoozed until {until_date}",
        "next_action": f"snoozed until {until_date}",
    }
    return update_fields(conn, job_id, updates)


def set_best_contact_for_job(
    conn: sqlite3.Connection, job_id: int, linkedin_url: str
) -> dict[str, str] | None:
    """Pin a specific contact as the job's best contact (spec 016 port of
    set_best_contact_for_job); returns None when the contact is not linked
    to the job."""
    row = get_job(conn, job_id)
    contacts = list_contacts(conn)
    linked = contacts_for_job(
        contacts, row.get("company", ""), row.get("title", ""), row.get("url", "")
    )
    chosen = None
    for contact in linked:
        if normalize(contact.get("linkedin_url", "")) == normalize(linkedin_url):
            chosen = contact
            break
    if chosen is None:
        return None
    updates = {
        "best_contact_name": chosen.get("person_name", ""),
        "best_contact_linkedin": chosen.get("linkedin_url", ""),
        "contacts_found": str(len(linked)),
        "outreach_status": (row.get("outreach_status", "") or "").strip() or "ready",
    }
    return update_fields(conn, job_id, updates)


def filter_outreach_rows(
    rows: list[dict[str, str]], *, due_only: bool = True
) -> list[dict[str, str]]:
    due: list[dict[str, str]] = []
    for row in rows:
        if normalize(row.get("status", "")) != "applied":
            continue
        action = normalize(row.get("next_outreach_action", ""))
        if not action:
            continue
        if due_only and action not in DUE_OUTREACH_ACTIONS:
            continue
        due.append(dict(row))
    return due


def outreach_due_rows(conn: sqlite3.Connection) -> list[dict[str, str]]:
    return filter_outreach_rows(sync_tracker_outreach(conn), due_only=True)
