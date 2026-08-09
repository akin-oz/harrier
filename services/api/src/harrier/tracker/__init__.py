"""Tracker: the single source of truth for application state (ADR-003).

All mutation goes through harrier.tracker.store; nothing else opens the
database for writing.
"""

from harrier.tracker.schema import (
    CONTACT_FIELDS,
    NEXT_ACTION_DEFAULTS,
    NOTE_KEYS,
    STATUSES,
    TRACKER_FIELDS,
)
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
    "CONTACT_FIELDS",
    "NEXT_ACTION_DEFAULTS",
    "NOTE_KEYS",
    "STATUSES",
    "TRACKER_FIELDS",
    "DuplicateJobError",
    "JobNotFoundError",
    "TrackerError",
    "UnknownStatusError",
    "add_contact",
    "add_job",
    "delete_contact",
    "get_job",
    "list_contacts",
    "list_jobs",
    "set_status",
    "update_contact_fields",
    "update_fields",
]
