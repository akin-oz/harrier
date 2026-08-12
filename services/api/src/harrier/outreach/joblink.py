"""Contacts reference jobs by identity, not by matching their text (spec 036).

A contact's `linked_jobs` held `{company, job_title, job_url}` and nothing
else, so the link between a person and the job they were contacted about was
a text comparison. Editing a job's title broke it silently: nothing errors,
the contact simply stops being about that job, and the failure shows up as an
outreach note that no longer belongs to anything.

The fix is a real key. Each link now carries `job_id`, resolved once when the
link is made, and the text fields stay for display and for links that could
not be resolved. They also stay because the tracker is not the only place a
contact can come from: a link to a job that was never tracked has no id to
carry, and dropping it would lose the record of who was contacted.

So an unresolved link is kept and reported, never discarded. `harrier check`
is where it is reported, alongside the row invariants, because both answer
the same question: what in this tracker no longer says something true.
"""

from __future__ import annotations

import sqlite3

from harrier.screening.normalized import normalize

# The link fields, in the order a reader wants them: the key first.
LINK_FIELDS: tuple[str, ...] = ("job_id", "company", "job_title", "job_url")


def resolve_job_id(conn: sqlite3.Connection, link: dict[str, str]) -> str:
    """The tracked job a link refers to, or "" when there is not one.

    URL first, then company and title, which is the order `find_duplicate`
    uses. Matching the dedupe order matters: a link resolved by a different
    rule than the one that decided two rows were the same job could point at
    the row that lost.
    """
    from harrier.tracker.store import list_jobs

    url = (link.get("job_url") or "").strip()
    company = normalize(link.get("company") or "")
    title = normalize(link.get("job_title") or "")
    if not url and not (company and title):
        return ""
    for job in list_jobs(conn):
        if url and job["url"].strip() == url:
            return str(job["id"])
    if company and title:
        for job in list_jobs(conn):
            if normalize(job["company"]) == company and normalize(job["title"]) == title:
                return str(job["id"])
    return ""


def job_for_link(conn: sqlite3.Connection, link: dict[str, str]) -> dict[str, str] | None:
    """The job a link points at, followed by its id rather than its text.

    This is what makes the link survive an edit: the title in the link may be
    stale, and the row it names is still the right row.
    """
    from harrier.tracker.store import get_job

    identifier = (link.get("job_id") or "").strip()
    if not identifier:
        return None
    try:
        return get_job(conn, int(identifier))
    except (ValueError, LookupError):
        # A link to a job that has since been deleted. Reported by
        # `unresolved_links`, not repaired here: deciding what a contact
        # about a deleted job means is not this function's business.
        return None


def unresolved_links(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Every contact link that does not resolve to a job, as (contact, why).

    Reporting only. A link may be unresolved because the job was never
    tracked, which is legitimate, or because the backfill could not match it,
    which is worth a human look. Both are listed: this cannot tell them
    apart, and guessing would hide the second behind the first.
    """
    from harrier.outreach.contacts import parse_linked_jobs
    from harrier.tracker.store import list_contacts

    problems: list[tuple[str, str]] = []
    for contact in list_contacts(conn):
        name = contact.get("person_name") or contact.get("linkedin_url") or contact.get("id", "?")
        for link in parse_linked_jobs(contact.get("linked_jobs", "")):
            if not (link.get("job_id") or "").strip():
                problems.append((str(name), "a linked job that matches no tracked job"))
            elif job_for_link(conn, link) is None:
                problems.append((str(name), f"a link to job {link['job_id']}, which is gone"))
    return problems


def backfill_job_ids(conn: sqlite3.Connection) -> tuple[int, int]:
    """Give every existing link an id where one can be found.

    Returns (resolved, left alone). Runs over links written before this
    existed. Nothing is dropped: a link that cannot be matched keeps its text
    and is reported by `unresolved_links`, because a contact record is
    somebody the operator spoke to and losing it to a schema change would be
    worse than carrying a link that no longer resolves.
    """
    from harrier.outreach.contacts import parse_linked_jobs, serialize_linked_jobs
    from harrier.tracker.store import list_contacts, update_contact_fields

    resolved = 0
    untouched = 0
    for contact in list_contacts(conn):
        links = parse_linked_jobs(contact.get("linked_jobs", ""))
        if not links:
            continue
        changed = False
        for link in links:
            if (link.get("job_id") or "").strip():
                continue
            found = resolve_job_id(conn, link)
            if found:
                link["job_id"] = found
                resolved += 1
                changed = True
            else:
                untouched += 1
        if changed:
            update_contact_fields(
                conn, int(contact["id"]), {"linked_jobs": serialize_linked_jobs(links)}
            )
    return resolved, untouched
