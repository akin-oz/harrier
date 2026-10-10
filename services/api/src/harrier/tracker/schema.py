"""Tracker schema: the single definition.

The old repo triplicated this list across scripts/job_sources.py,
scripts/jobs.py, and gui/constants.py; here it exists once. Field order is the
legacy CSV column order and is load-bearing for export fidelity.
"""

from __future__ import annotations

from harrier.tracks import KIND_RULES, TRACK_KINDS

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
    # The policy version that produced the score (spec 033). A bare number
    # cannot say whether it is comparable with the row above it.
    "scoring_version",
    "remote_filter",
    # `manual_reject` was here. Nothing ever wrote it: not the CLI, not the
    # API, not an importer. It was a column, a response field and a published
    # contract field describing a decision the product does not record, and a
    # reader who trusted it would have concluded no rejection was ever manual
    # (spec 036). Removed rather than given a writer, because a rejection
    # reason already says who decided and why.
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

# The industry kind's next actions (old repo: scripts/jobs.py
# NEXT_ACTION_DEFAULTS). One definition, in `harrier.tracks.KIND_RULES`, so a
# kind's defaults and this name cannot drift (spec 093).
NEXT_ACTION_DEFAULTS: dict[str, str] = dict(KIND_RULES["industry"].next_action)

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
_TRACK_KIND_LIST = ", ".join(f"'{kind}'" for kind in TRACK_KINDS)
_JOB_TEXT_COLUMNS = ", ".join(
    f"{name} TEXT NOT NULL DEFAULT ''" for name in TRACKER_FIELDS if name != "status"
)
# Migration 1 is history and must describe the table as it was first created.
# Deriving its column list from the live NOTE_KEYS meant that adding a key
# both changed what a fresh database got at migration 1 and left the later
# ALTER to run against a column that already existed, so a fresh install
# failed on "duplicate column name" while an existing one worked. The live
# list stays the description of the table; this is the record of its first
# version. `tests/test_scoring.py::test_a_migrated_database_matches_a_fresh_one`
# holds the two paths together.
_ORIGINAL_NOTE_KEYS: tuple[str, ...] = (
    "score",
    "archetype",
    "source_label",
    "external_key",
    "signals",
    "remote_filter",
    "manual_reject",
    "manual_added",
)
_PROMOTED_COLUMNS = ", ".join(f"{name} TEXT NOT NULL DEFAULT ''" for name in _ORIGINAL_NOTE_KEYS)
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
    (
        3,
        [
            # The scoring version, promoted alongside the score (spec 033).
            # A fresh database gets this column from NOTE_KEYS at migration 1;
            # this is the same column for a database that already exists.
            # `tests/test_scoring.py::test_a_migrated_database_matches_a_fresh_one`
            # holds the two paths together.
            "ALTER TABLE jobs ADD COLUMN scoring_version TEXT NOT NULL DEFAULT ''",
        ],
    ),
    (
        4,
        [
            # When each scheduled job last completed successfully (spec 029).
            # A job that fails loudly is visible in its exit status; a job
            # that hangs, or that the scheduler stopped starting, produces no
            # status at all, and the only evidence is the absence of a recent
            # success.
            #
            # Job names and timestamps only. Nothing here describes what a run
            # found, so the table carries no personal data (ADR-008).
            """
            CREATE TABLE job_runs (
                job TEXT PRIMARY KEY,
                last_success_at TEXT NOT NULL
            )
            """,
        ],
    ),
    (
        5,
        [
            # Numbered 5, not 4: migration 4 is spec 029's `job_runs`, already
            # on main. The runner skips any version at or below the recorded
            # one, so two migrations sharing a number, or a later one with a
            # lower number, means one silently never runs.
            #
            # `manual_reject` had no writer anywhere (spec 036). Dropping the
            # column is safe precisely because of that: there is nothing in
            # it to lose. It stays in `_ORIGINAL_NOTE_KEYS` above, because
            # migration 1 records the table as it was first created and
            # rewriting history would break a database replaying from empty.
            "ALTER TABLE jobs DROP COLUMN manual_reject",
        ],
    ),
    (
        6,
        [
            # `scope` never held anything but 'default'. It was threaded
            # through eight signatures and guarded the one table that holds
            # no personal data, while the tables that do hold it had no
            # equivalent, so it did not buy the tenancy it was there for
            # (spec 041). Removed rather than extended: adding an unused
            # column to every personal-data table is more speculative
            # generality, not less. Re-adding it later is a migration, and
            # ADR-009 now says so.
            #
            # A rebuild rather than DROP COLUMN, because the unique index
            # names the column.
            """
            CREATE TABLE user_config_new (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                value TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (kind)
            )
            """,
            """
            INSERT INTO user_config_new (id, kind, value, updated_at)
            SELECT id, kind, value, updated_at FROM user_config
            WHERE scope = 'default'
            """,
            "DROP TABLE user_config",
            "ALTER TABLE user_config_new RENAME TO user_config",
        ],
    ),
    (
        7,
        [
            # Every decision on a job, with who made it and why (spec 079).
            # The `jobs` row says where a job is; this says how it got there,
            # which `set_status` used to overwrite. Written only by the
            # tracker write path, in the same transaction as the row change.
            #
            # Numbered 7 because 5 and 6 are taken; spec 079 said 5 before
            # it was checked against main, and its amendment records that.
            #
            # The CHECK ties an outcome to the company and nothing else: a
            # company's verdict can never be stored as the candidate's
            # decision, whatever the caller passes.
            """
            CREATE TABLE job_events (
                id INTEGER PRIMARY KEY,
                job_id INTEGER NOT NULL REFERENCES jobs(id),
                at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                kind TEXT NOT NULL CHECK (kind IN ('created', 'decision', 'outcome')),
                actor TEXT NOT NULL
                    CHECK (actor IN ('candidate', 'company', 'system', 'unknown')),
                from_status TEXT NOT NULL DEFAULT '',
                to_status TEXT NOT NULL,
                reason_code TEXT NOT NULL DEFAULT '',
                reason_text TEXT NOT NULL DEFAULT '',
                fit_score TEXT NOT NULL DEFAULT '',
                scoring_version TEXT NOT NULL DEFAULT '',
                description_sha256 TEXT NOT NULL DEFAULT '',
                backfilled INTEGER NOT NULL DEFAULT 0 CHECK (backfilled IN (0, 1)),
                CHECK ((kind = 'outcome') = (actor = 'company'))
            )
            """,
            "CREATE INDEX idx_job_events_job ON job_events(job_id, at)",
            # Append-only in the database, not by convention. A correction is
            # a new event; an edit would let history say something it never
            # recorded.
            """
            CREATE TRIGGER job_events_append_only_update BEFORE UPDATE ON job_events
            BEGIN SELECT RAISE(ABORT, 'job_events is append-only'); END
            """,
            """
            CREATE TRIGGER job_events_append_only_delete BEFORE DELETE ON job_events
            BEGIN SELECT RAISE(ABORT, 'job_events is append-only'); END
            """,
        ],
    ),
    (
        8,
        [
            # Search tracks (spec 091, ADR-012). A track is a second kind of
            # search by the same person; a tenant is a second person and is a
            # store boundary, never a column. The kind list derives from
            # TRACK_KINDS the way the status CHECK derives from STATUSES.
            f"""
            CREATE TABLE tracks (
                id INTEGER PRIMARY KEY,
                slug TEXT NOT NULL UNIQUE CHECK (
                    length(slug) BETWEEN 1 AND 32
                    AND slug GLOB '[a-z]*'
                    AND slug NOT GLOB '*[^a-z0-9-]*'
                ),
                kind TEXT NOT NULL CHECK (kind IN ({_TRACK_KIND_LIST})),
                label TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                archived_at TEXT
            )
            """,
            # The one track every existing row belongs to. Its slug and label
            # are rows like any other track's; only its id is fixed.
            (
                "INSERT INTO tracks (id, slug, kind, label) "
                "VALUES (1, 'job', 'industry', 'Job search')"
            ),
            # ADD COLUMN, so `jobs` is never rebuilt: `job_events` references
            # it and the runner cannot turn foreign keys off inside its
            # transaction (spec 090). SQLite refuses a REFERENCES clause on an
            # added column whose default is not NULL, so the referential rule
            # is two triggers rather than a foreign key.
            "ALTER TABLE jobs ADD COLUMN track_id INTEGER NOT NULL DEFAULT 1",
            """
            CREATE TRIGGER jobs_track_must_exist_on_insert BEFORE INSERT ON jobs
            BEGIN
                SELECT RAISE(ABORT, 'unknown track')
                WHERE NOT EXISTS (SELECT 1 FROM tracks WHERE id = NEW.track_id);
            END
            """,
            """
            CREATE TRIGGER jobs_track_must_exist_on_update BEFORE UPDATE OF track_id ON jobs
            BEGIN
                SELECT RAISE(ABORT, 'unknown track')
                WHERE NOT EXISTS (SELECT 1 FROM tracks WHERE id = NEW.track_id);
            END
            """,
            # Archiving (spec 093) is the only lifecycle verb a track has, so
            # no job row can ever name a track that is gone.
            """
            CREATE TRIGGER tracks_are_never_deleted BEFORE DELETE ON tracks
            BEGIN SELECT RAISE(ABORT, 'tracks are archived, never deleted'); END
            """,
            # An id update runs none of the job triggers and would leave every
            # row of that track naming a number no track has (review of PR
            # #180). The id is the one column a job row points at.
            """
            CREATE TRIGGER tracks_keep_their_id BEFORE UPDATE OF id ON tracks
            BEGIN SELECT RAISE(ABORT, 'a track id never changes'); END
            """,
            "CREATE INDEX idx_jobs_track_status ON jobs(track_id, status)",
        ],
    ),
    (
        9,
        [
            # The one date an academic call is ordered by (spec 093). Empty
            # means no deadline. SQLite tests a CHECK on an added column
            # against every existing row; every existing row has the default.
            # The CHECK holds the shape; the CLI parser holds that the date
            # exists.
            """
            ALTER TABLE jobs ADD COLUMN deadline TEXT NOT NULL DEFAULT ''
            CHECK (
                deadline = ''
                OR (
                    length(deadline) = 10
                    AND deadline GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
                )
            )
            """,
        ],
    ),
    (
        10,
        [
            # A profile document is owned by one track or shared by all
            # (spec 099, spec 091's design note). NULL is shared. SQLite
            # treats NULLs as distinct in a plain UNIQUE, so uniqueness is two
            # partial indexes. A rebuild, as migration 7, because the inline
            # UNIQUE (kind, name) cannot be dropped. `track_id` goes last, as
            # Postgres's ADD COLUMN puts it, so the two stores keep one column
            # order (spec 103).
            """
            CREATE TABLE profile_documents_new (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                format TEXT NOT NULL DEFAULT 'text',
                content TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                track_id INTEGER REFERENCES tracks(id)
            )
            """,
            # The one framing that can exist is the default track's (spec 098).
            """
            INSERT INTO profile_documents_new
                (id, kind, name, format, content, updated_at, track_id)
            SELECT id, kind, name, format, content, updated_at,
                CASE kind WHEN 'resume_framing' THEN 1 END
            FROM profile_documents
            """,
            "DROP TABLE profile_documents",
            "ALTER TABLE profile_documents_new RENAME TO profile_documents",
            """
            CREATE UNIQUE INDEX idx_profile_documents_shared
            ON profile_documents (kind, name) WHERE track_id IS NULL
            """,
            """
            CREATE UNIQUE INDEX idx_profile_documents_owned
            ON profile_documents (track_id, kind, name) WHERE track_id IS NOT NULL
            """,
        ],
    ),
]

# --- Postgres (spec 103, ADR-013) ---
#
# The hosted store's history begins here: one baseline, recorded as version
# 9, that creates the schema SQLite reaches after migration 9. Migrations 1
# to 9 above stay SQLite only; their rebuilds and triggers worked around
# SQLite limits a Postgres store never had. Every migration after 9 declares
# both dialects, one entry in each list with the same version, and
# `undeclared_dialects` names any version that has only one.
# `tests/test_dialect_parity.py` holds the two stores to the same tables,
# columns, defaults and refusals.

POSTGRES_BASELINE_VERSION = 9

# The runner's own bookkeeping, created before any migration runs. Here
# rather than in harrier.pgstore so this module holds every line of Postgres
# DDL (spec 103).
POSTGRES_VERSION_TABLE = "CREATE TABLE IF NOT EXISTS schema_version (version integer PRIMARY KEY)"

# SQLite's datetime('now') and strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), as
# text in the same shapes, so no value read through the API changes form.
_PG_NOW = "to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"
_PG_NOW_ISO = """to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')"""
_PG_ID = "id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"

# The jobs columns in the order SQLite has them after migration 9: migration
# 1's columns less the dropped `manual_reject`, then each added column in the
# order its migration added it.
_PG_JOB_TEXT = ", ".join(
    f"{name} text NOT NULL DEFAULT ''" for name in TRACKER_FIELDS if name != "status"
)
_PG_JOB_PROMOTED = ", ".join(
    f"{name} text NOT NULL DEFAULT ''" for name in _ORIGINAL_NOTE_KEYS if name != "manual_reject"
)

POSTGRES_BASELINE: list[str] = [
    # One function serves every refusal trigger; the message is the trigger's
    # argument, the same text the SQLite trigger raises.
    """
    CREATE FUNCTION harrier_refuse() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION '%', TG_ARGV[0];
    END
    $$
    """,
    f"""
    CREATE TABLE tracks (
        {_PG_ID},
        slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z][a-z0-9-]{{0,31}}$'),
        kind text NOT NULL CHECK (kind IN ({_TRACK_KIND_LIST})),
        label text NOT NULL,
        created_at text NOT NULL DEFAULT {_PG_NOW},
        archived_at text
    )
    """,
    "INSERT INTO tracks (id, slug, kind, label) VALUES (1, 'job', 'industry', 'Job search')",
    # An identity column does not advance for an explicit id; without this
    # the next track inserted without one would be given 1 and collide.
    "SELECT setval(pg_get_serial_sequence('tracks', 'id'), (SELECT max(id) FROM tracks))",
    """
    CREATE TRIGGER tracks_are_never_deleted BEFORE DELETE ON tracks
    FOR EACH ROW EXECUTE FUNCTION harrier_refuse('tracks are archived, never deleted')
    """,
    """
    CREATE TRIGGER tracks_keep_their_id BEFORE UPDATE OF id ON tracks
    FOR EACH ROW EXECUTE FUNCTION harrier_refuse('a track id never changes')
    """,
    f"""
    CREATE TABLE jobs (
        {_PG_ID},
        {_PG_JOB_TEXT},
        status text NOT NULL DEFAULT 'prospect' CHECK (status IN ({_STATUS_LIST})),
        {_PG_JOB_PROMOTED},
        created_at text NOT NULL DEFAULT {_PG_NOW},
        updated_at text NOT NULL DEFAULT {_PG_NOW},
        scoring_version text NOT NULL DEFAULT '',
        track_id bigint NOT NULL DEFAULT 1 REFERENCES tracks(id),
        deadline text NOT NULL DEFAULT '' CHECK (
            deadline = '' OR deadline ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$'
        )
    )
    """,
    "CREATE UNIQUE INDEX idx_jobs_url ON jobs(url) WHERE url <> ''",
    "CREATE UNIQUE INDEX idx_jobs_external_key ON jobs(external_key) WHERE external_key <> ''",
    "CREATE INDEX idx_jobs_company_title ON jobs(company, title)",
    "CREATE INDEX idx_jobs_status ON jobs(status)",
    "CREATE INDEX idx_jobs_track_status ON jobs(track_id, status)",
    f"""
    CREATE TABLE contacts (
        {_PG_ID},
        {_CONTACT_COLUMNS.replace("TEXT", "text")},
        created_at text NOT NULL DEFAULT {_PG_NOW},
        updated_at text NOT NULL DEFAULT {_PG_NOW}
    )
    """,
    "CREATE INDEX idx_contacts_company ON contacts(company)",
    f"""
    CREATE TABLE profile_documents (
        {_PG_ID},
        kind text NOT NULL,
        name text NOT NULL,
        format text NOT NULL DEFAULT 'text',
        content text NOT NULL DEFAULT '',
        updated_at text NOT NULL DEFAULT {_PG_NOW},
        UNIQUE (kind, name)
    )
    """,
    f"""
    CREATE TABLE user_config (
        {_PG_ID},
        kind text NOT NULL,
        value text NOT NULL DEFAULT '',
        updated_at text NOT NULL DEFAULT {_PG_NOW},
        UNIQUE (kind)
    )
    """,
    """
    CREATE TABLE job_runs (
        job text PRIMARY KEY,
        last_success_at text NOT NULL
    )
    """,
    f"""
    CREATE TABLE job_events (
        {_PG_ID},
        job_id bigint NOT NULL REFERENCES jobs(id),
        at text NOT NULL DEFAULT {_PG_NOW_ISO},
        kind text NOT NULL CHECK (kind IN ('created', 'decision', 'outcome')),
        actor text NOT NULL CHECK (actor IN ('candidate', 'company', 'system', 'unknown')),
        from_status text NOT NULL DEFAULT '',
        to_status text NOT NULL,
        reason_code text NOT NULL DEFAULT '',
        reason_text text NOT NULL DEFAULT '',
        fit_score text NOT NULL DEFAULT '',
        scoring_version text NOT NULL DEFAULT '',
        description_sha256 text NOT NULL DEFAULT '',
        backfilled integer NOT NULL DEFAULT 0 CHECK (backfilled IN (0, 1)),
        CHECK ((kind = 'outcome') = (actor = 'company'))
    )
    """,
    "CREATE INDEX idx_job_events_job ON job_events(job_id, at)",
    """
    CREATE TRIGGER job_events_append_only_update BEFORE UPDATE ON job_events
    FOR EACH ROW EXECUTE FUNCTION harrier_refuse('job_events is append-only')
    """,
    """
    CREATE TRIGGER job_events_append_only_delete BEFORE DELETE ON job_events
    FOR EACH ROW EXECUTE FUNCTION harrier_refuse('job_events is append-only')
    """,
]

POSTGRES_MIGRATIONS: list[tuple[int, list[str]]] = [
    (POSTGRES_BASELINE_VERSION, POSTGRES_BASELINE),
    (
        10,
        [
            # Spec 099: SQLite's migration 10, without its rebuild. The
            # baseline's UNIQUE (kind, name) is the default-named constraint.
            "ALTER TABLE profile_documents ADD COLUMN track_id bigint REFERENCES tracks(id)",
            "ALTER TABLE profile_documents DROP CONSTRAINT profile_documents_kind_name_key",
            "UPDATE profile_documents SET track_id = 1 WHERE kind = 'resume_framing'",
            """
            CREATE UNIQUE INDEX idx_profile_documents_shared
            ON profile_documents (kind, name) WHERE track_id IS NULL
            """,
            """
            CREATE UNIQUE INDEX idx_profile_documents_owned
            ON profile_documents (track_id, kind, name) WHERE track_id IS NOT NULL
            """,
        ],
    ),
]


def undeclared_dialects(
    sqlite: list[tuple[int, list[str]]],
    postgres: list[tuple[int, list[str]]],
    baseline: int = POSTGRES_BASELINE_VERSION,
) -> list[str]:
    """Every version after the baseline that one dialect lacks or leaves empty.

    A migration that lands for one store only makes the two drift apart in a
    way the parity test may not see, so each later version must appear in
    both lists with at least one statement (spec 103).
    """
    sqlite_versions = {version: statements for version, statements in sqlite if version > baseline}
    postgres_versions = {
        version: statements for version, statements in postgres if version > baseline
    }
    problems: list[str] = []
    for version in sorted(set(sqlite_versions) | set(postgres_versions)):
        for dialect, versions in (("sqlite", sqlite_versions), ("postgres", postgres_versions)):
            if not versions.get(version):
                problems.append(f"migration {version} declares no {dialect} statements")
    return problems
