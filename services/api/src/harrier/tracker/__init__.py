"""Tracker: the single source of truth for application state (ADR-003).

All mutation goes through harrier.tracker.store; nothing else opens the
database for writing.
"""

from harrier.tracker.queue import ACTIVE_STATUSES, rank_active, status_counts
from harrier.tracker.schema import (
    CONTACT_FIELDS,
    NEXT_ACTION_DEFAULTS,
    NOTE_KEYS,
    STATUSES,
    TRACKER_FIELDS,
)
from harrier.tracker.selector import SelectorError, describe, resolve_selector
from harrier.tracker.store import (
    DuplicateJobError,
    JobNotFoundError,
    TrackerError,
    UnknownStatusError,
    add_contact,
    add_job,
    delete_contact,
    get_job,
    list_contacts,
    list_jobs,
    set_status,
    update_contact_fields,
    update_fields,
)

__all__ = [
    "ACTIVE_STATUSES",
    "CONTACT_FIELDS",
    "NEXT_ACTION_DEFAULTS",
    "NOTE_KEYS",
    "STATUSES",
    "TRACKER_FIELDS",
    "DuplicateJobError",
    "JobNotFoundError",
    "SelectorError",
    "TrackerError",
    "UnknownStatusError",
    "add_contact",
    "add_job",
    "delete_contact",
    "describe",
    "get_job",
    "list_contacts",
    "list_jobs",
    "rank_active",
    "resolve_selector",
    "set_status",
    "status_counts",
    "update_contact_fields",
    "update_fields",
]
