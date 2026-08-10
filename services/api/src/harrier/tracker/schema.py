"""Tracker schema: the single definition.

The old repo triplicated this list across scripts/job_sources.py,
scripts/jobs.py, and gui/constants.py; here it exists once. Field order is the
legacy CSV column order and is load-bearing for export fidelity.
"""

from __future__ import annotations

# Legacy 20-column order (old repo: scripts/job_sources.py TRACKER_FIELDS).
TRACKER_FIELDS: tuple[str, ...] = (
    "company",
    "title",
    "location",
    "url",
    "source",
    "added_at",
    "fit_score",
    "status",
    "applied_date",
    "last_contact",
    "next_action",
    "outreach_status",
    "last_outreach_at",
    "next_outreach_action",
    "best_contact_name",
    "best_contact_linkedin",
    "contacts_found",
    "outreach_priority",
    "rejection_reason",
    "notes",
)

# Keys promoted out of the notes key=value store into real columns (ADR-003).
NOTE_KEYS: tuple[str, ...] = (
    "score",
    "archetype",
    "source_label",
    "external_key",
    "signals",
    "remote_filter",
    "manual_reject",
    "manual_added",
)

STATUSES: tuple[str, ...] = (
    "prospect",
    "shortlisted",
    "tailored_cv_requested",
    "applied",
    "interviewing",
    "rejected",
)

# Old repo: scripts/jobs.py NEXT_ACTION_DEFAULTS.
NEXT_ACTION_DEFAULTS: dict[str, str] = {
    "prospect": "review and decide whether to apply",
    "shortlisted": "request tailored CV and review before applying",
    "tailored_cv_requested": "review tailored PDF before applying",
    "applied": "follow up if no reply within 7 days",
    "interviewing": "prepare for interview",
    "rejected": "",
}

# Legacy 17-column order (old repo: scripts/outreach_lib.py CONTACT_FIELDS).
CONTACT_FIELDS: tuple[str, ...] = (
    "company",
    "applied_job_title",
    "job_url",
    "linked_jobs",
    "person_name",
    "person_title",
    "person_email",
    "relevance",
    "fit_score",
    "fit_reason",
    "location",
    "source",
    "linkedin_url",
    "contact_status",
    "reply_status",
    "last_contacted_at",
    "notes",
)

_STATUS_LIST = ", ".join(f"'{s}'" for s in STATUSES)
_JOB_TEXT_COLUMNS = ", ".join(
    f"{name} TEXT NOT NULL DEFAULT ''" for name in TRACKER_FIELDS if name != "status"
)
_PROMOTED_COLUMNS = ", ".join(f"{name} TEXT NOT NULL DEFAULT ''" for name in NOTE_KEYS)
_CONTACT_COLUMNS = ", ".join(f"{name} TEXT NOT NULL DEFAULT ''" for name in CONTACT_FIELDS)

MIGRATIONS: list[tuple[int, list[str]]] = [
    (
        1,
        [
            f"""
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY,
                {_JOB_TEXT_COLUMNS},
                status TEXT NOT NULL DEFAULT 'prospect' CHECK (status IN ({_STATUS_LIST})),
                {_PROMOTED_COLUMNS},
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """,
            "CREATE UNIQUE INDEX idx_jobs_url ON jobs(url) WHERE url != ''",
            (
                "CREATE UNIQUE INDEX idx_jobs_external_key ON jobs(external_key) "
                "WHERE external_key != ''"
            ),
            "CREATE INDEX idx_jobs_company_title ON jobs(company, title)",
            "CREATE INDEX idx_jobs_status ON jobs(status)",
            f"""
            CREATE TABLE contacts (
                id INTEGER PRIMARY KEY,
                {_CONTACT_COLUMNS},
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """,
            "CREATE INDEX idx_contacts_company ON contacts(company)",
            """
            CREATE TABLE profile_documents (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                format TEXT NOT NULL DEFAULT 'text',
                content TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (kind, name)
            )
            """,
        ],
    ),
    (
        2,
        [
            # User configuration: the board watchlist, LinkedIn searches,
            # discovery settings, and the hold list (spec 023, ADR-009).
            # These were gitignored loose files; they are user data, and the
            # database is where user data lives (ADR-008).
            #
            # scope is the tenancy seam. It is 'default' everywhere today and
            # nothing reads it as a variable, but the unique key includes it,
            # so partitioning later is a query change rather than a migration
            # of every row (ADR-009: tenant-ready, not tenant-complete).
            """
            CREATE TABLE user_config (
                id INTEGER PRIMARY KEY,
                scope TEXT NOT NULL DEFAULT 'default',
                kind TEXT NOT NULL,
                value TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (scope, kind)
            )
            """,
        ],
    ),
]
