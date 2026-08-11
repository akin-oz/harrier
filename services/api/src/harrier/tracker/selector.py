"""Naming one tracker row without editing the wrong one (spec 027).

Every mutating verb takes a selector, so this is the single place where
"which job did you mean" is decided. Its failure mode is the reason it is
its own module with its own tests: guessing here silently rewrites somebody
else's application.

Two forms, matching the old CLI:

- a number, which is the job id
- a substring, matched against company, title and URL

Ambiguity is never resolved by picking one. It aborts and lists what
matched, so the operator narrows the selector themselves.
"""

from __future__ import annotations

import sqlite3

from harrier.screening.normalized import normalize
from harrier.tracker.store import TrackerError, list_jobs

# How many candidates an ambiguous selector prints before truncating. The
# point is to help the operator narrow it, not to page the whole tracker.
AMBIGUITY_LIMIT = 10


class SelectorError(TrackerError):
    """The selector named no row, or more than one."""


def describe(job: dict[str, str]) -> str:
    return f"{job['id']}. {job['company']} - {job['title']} [{job['status']}]"


def resolve_selector(conn: sqlite3.Connection, selector: str) -> dict[str, str]:
    """The one row this selector names.

    A numeric selector is the job id, not a position. The old CLI indexed
    into the CSV by row number, which moved whenever a row above it was
    added or removed; ids do not, and they are what the API and the web app
    already show (stated change from the old code, spec 027).
    """
    cleaned = selector.strip()
    if not cleaned:
        raise SelectorError("empty selector")

    jobs = list_jobs(conn)
    if cleaned.isdigit():
        wanted = int(cleaned)
        for job in jobs:
            if int(job["id"]) == wanted:
                return job
        raise SelectorError(f"no job with id {wanted}")

    needle = normalize(cleaned)
    matches = [
        job
        for job in jobs
        if needle in normalize(f"{job['company']} | {job['title']} | {job['url']}")
    ]
    if not matches:
        raise SelectorError(f"no tracker rows match selector: {selector}")
    if len(matches) > 1:
        lines = [f"selector is ambiguous: {selector}", ""]
        lines.extend(describe(job) for job in matches[:AMBIGUITY_LIMIT])
        if len(matches) > AMBIGUITY_LIMIT:
            lines.append(f"... and {len(matches) - AMBIGUITY_LIMIT} more")
        lines.append("")
        lines.append("Narrow the selector, or use the numeric id.")
        raise SelectorError("\n".join(lines))
    return matches[0]
