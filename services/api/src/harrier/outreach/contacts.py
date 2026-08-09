"""DB-backed contact operations (spec 016 port of outreach_lib.py's
contact half).

The applied_job_title vs person_title split is load-bearing: the first
is the job the operator applied to, the second is the contact's own
title. linked_jobs is a json list of {company, job_title, job_url} so
one person can cover several applications.
"""

from __future__ import annotations

import json
import sqlite3
from typing import cast

from harrier.tracker import (
    add_contact,
    list_contacts,
    update_contact_fields,
)
from harrier.tracker import (
    delete_contact as tracker_delete_contact,
)

RELEVANCE_ORDER = {
    "recruiter": 0,
    "hiring_manager": 1,
    "eng_manager": 2,
    "team_lead": 3,
    "team_member": 4,
    "founder_cto": 5,
    "founder": 5,
    "other": 6,
}


def normalize(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def infer_relevance(role_title: str) -> str:
    title = normalize(role_title)
    if any(token in title for token in ("recruiter", "talent", "sourcer", "people partner")):
        return "recruiter"
    if "hiring manager" in title:
        return "hiring_manager"
    if any(
        token in title
        for token in (
            "engineering manager",
            "eng manager",
            "head of engineering",
            "director of engineering",
            "vp engineering",
        )
    ):
        return "eng_manager"
    if any(
        token in title
        for token in (
            "frontend lead",
            "web lead",
            "tech lead",
            "team lead",
            "lead engineer",
            "lead frontend",
        )
    ):
        return "team_lead"
    if any(
        token in title
        for token in ("founder", "co-founder", "ceo", "cto", "chief technology officer")
    ):
        return "founder_cto"
    return "other"


def normalize_job_link(company: str, role: str, job_url: str) -> dict[str, str]:
    return {
        "company": (company or "").strip(),
        "job_title": (role or "").strip(),
        "job_url": (job_url or "").strip(),
    }


def parse_linked_jobs(value: str) -> list[dict[str, str]]:
    text = (value or "").strip()
    if not text:
        return []
    try:
        payload: object = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    normalized: list[dict[str, str]] = []
    for item in cast("list[object]", payload):
        if not isinstance(item, dict):
            continue
        entry = cast("dict[str, object]", item)
        normalized.append(
            normalize_job_link(
                str(entry.get("company", "")),
                str(entry.get("job_title", "")),
                str(entry.get("job_url", "")),
            )
        )
    return normalized


def serialize_linked_jobs(items: list[dict[str, str]]) -> str:
    return json.dumps(items, ensure_ascii=False)


def merge_contact_link(contact: dict[str, str], company: str, role: str, job_url: str) -> None:
    """Merge a job link into the contact, keeping the newest as the direct
    fields and every distinct link in linked_jobs."""
    new_link = normalize_job_link(company, role, job_url)
    links = parse_linked_jobs(contact.get("linked_jobs", ""))
    if not links and (
        contact.get("company") or contact.get("applied_job_title") or contact.get("job_url")
    ):
        links.append(
            normalize_job_link(
                contact.get("company", ""),
                contact.get("applied_job_title", ""),
                contact.get("job_url", ""),
            )
        )
    seen: set[tuple[str, str, str]] = set()
    merged: list[dict[str, str]] = []
    for item in [*links, new_link]:
        key = (
            normalize(item.get("company", "")),
            normalize(item.get("job_title", "")),
            normalize(item.get("job_url", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    contact["linked_jobs"] = serialize_linked_jobs(merged)
    contact["company"] = new_link["company"]
    contact["applied_job_title"] = new_link["job_title"]
    contact["job_url"] = new_link["job_url"]


def contact_key(contact: dict[str, str]) -> str:
    return normalize(contact.get("linkedin_url", "") or contact.get("person_name", ""))


def find_contact(conn: sqlite3.Connection, identifier: str) -> dict[str, str] | None:
    """Match by linkedin_url first, then person_name. An empty identifier
    matches nothing (review finding: "" must never select a row)."""
    id_norm = normalize(identifier)
    if not id_norm:
        return None
    rows = list_contacts(conn)
    for row in rows:
        if normalize(row.get("linkedin_url", "")) == id_norm:
            return row
    for row in rows:
        if normalize(row.get("person_name", "")) == id_norm:
            return row
    return None


def upsert_contact(
    conn: sqlite3.Connection,
    *,
    company: str,
    role: str,
    job_url: str,
    person_name: str,
    person_title: str,
    linkedin_url: str,
    person_email: str = "",
    location: str = "",
    source: str,
    relevance: str = "",
    fit_score: str = "",
    fit_reason: str = "",
    notes: str = "",
) -> dict[str, str]:
    """The single write path for contacts (spec 016): merges by normalized
    linkedin_url (or person_name) and merges linked_jobs across jobs."""
    relevance = relevance or infer_relevance(person_title)
    key = normalize(linkedin_url or person_name)
    if not key:
        raise ValueError("contact needs a linkedin_url or person_name identity")
    for existing in list_contacts(conn):
        if contact_key(existing) != key:
            continue
        # Empty incoming values never clobber stored ones (review finding).
        updates = {
            "person_name": person_name or existing.get("person_name", ""),
            "person_title": person_title or existing.get("person_title", ""),
            "person_email": person_email or existing.get("person_email", ""),
            "relevance": relevance,
            "fit_score": fit_score or existing.get("fit_score", ""),
            "fit_reason": fit_reason or existing.get("fit_reason", ""),
            "location": location or existing.get("location", ""),
            "source": source or existing.get("source", ""),
            "linkedin_url": linkedin_url or existing.get("linkedin_url", ""),
        }
        if notes:
            existing_notes = (existing.get("notes") or "").strip()
            updates["notes"] = f"{existing_notes}; {notes}".strip("; ") if existing_notes else notes
        merged = {**existing, **updates}
        merge_contact_link(merged, company, role, job_url)
        updates["linked_jobs"] = merged["linked_jobs"]
        updates["company"] = merged["company"]
        updates["applied_job_title"] = merged["applied_job_title"]
        updates["job_url"] = merged["job_url"]
        update_contact_fields(conn, int(existing["id"]), updates)
        merged["id"] = existing["id"]
        return merged

    contact = {
        "company": company,
        "applied_job_title": role,
        "job_url": job_url,
        "linked_jobs": "",
        "person_name": person_name,
        "person_title": person_title,
        "person_email": person_email,
        "relevance": relevance,
        "fit_score": fit_score,
        "fit_reason": fit_reason,
        "location": location,
        "source": source,
        "linkedin_url": linkedin_url,
        "contact_status": "new",
        "reply_status": "",
        "last_contacted_at": "",
        "notes": notes,
    }
    merge_contact_link(contact, company, role, job_url)
    contact_id = add_contact(conn, contact)
    contact["id"] = str(contact_id)
    return contact


def update_contact(
    conn: sqlite3.Connection, identifier: str, updates: dict[str, str]
) -> dict[str, str] | None:
    target = find_contact(conn, identifier)
    if target is None:
        return None
    update_contact_fields(conn, int(target["id"]), updates)
    return {**target, **updates}


def delete_contact(conn: sqlite3.Connection, identifier: str) -> bool:
    target = find_contact(conn, identifier)
    if target is None:
        return False
    return tracker_delete_contact(conn, int(target["id"]))


def update_contact_status(
    conn: sqlite3.Connection,
    linkedin_url: str,
    *,
    contact_status: str | None = None,
    reply_status: str | None = None,
    contacted_at: str | None = None,
    notes: str | None = None,
) -> dict[str, str] | None:
    target = find_contact(conn, linkedin_url)
    if target is None:
        return None
    updates: dict[str, str] = {}
    if contact_status is not None:
        updates["contact_status"] = contact_status
    if reply_status is not None:
        updates["reply_status"] = reply_status
    if contacted_at is not None:
        updates["last_contacted_at"] = contacted_at
    if notes:
        existing = (target.get("notes") or "").strip()
        updates["notes"] = f"{existing}; {notes}".strip("; ") if existing else notes
    if updates:
        update_contact_fields(conn, int(target["id"]), updates)
    return {**target, **updates}


def contact_sort_key(contact: dict[str, str]) -> tuple[int, int, str, str]:
    relevance = contact.get("relevance", "") or infer_relevance(contact.get("person_title", ""))
    try:
        fit_score = -int(str(contact.get("fit_score", "")).strip() or "0")
    except ValueError:
        fit_score = 0
    return (
        RELEVANCE_ORDER.get(relevance, 99),
        fit_score,
        normalize(contact.get("person_title", "")),
        normalize(contact.get("person_name", "")),
    )


def contacts_for_job(
    contacts: list[dict[str, str]], company: str, role: str, job_url: str
) -> list[dict[str, str]]:
    company_norm = normalize(company)
    role_norm = normalize(role)
    url_norm = normalize(job_url)
    matched: list[dict[str, str]] = []
    for row in contacts:
        direct_match = (url_norm and normalize(row.get("job_url", "")) == url_norm) or (
            normalize(row.get("company", "")) == company_norm
            and normalize(row.get("applied_job_title", "")) == role_norm
        )
        linked_match = any(
            (url_norm and normalize(item.get("job_url", "")) == url_norm)
            or (
                normalize(item.get("company", "")) == company_norm
                and normalize(item.get("job_title", "")) == role_norm
            )
            for item in parse_linked_jobs(row.get("linked_jobs", ""))
        )
        if direct_match or linked_match:
            matched.append(row)
    return matched


def best_contact(contacts: list[dict[str, str]]) -> dict[str, str] | None:
    if not contacts:
        return None
    return sorted(contacts, key=contact_sort_key)[0]
