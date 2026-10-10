"""Tracker schema: the single definition.

The old repo triplicated this list across scripts/job_sources.py,
scripts/jobs.py, and gui/constants.py; here it exists once. Field order is the
legacy CSV column order and is load-bearing for export fidelity.
"""

from __future__ import annotations

from typing import Literal

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
    # Postgres only (spec 105): the owner column and the row policy exist on
    # the hosted store alone, because the local schema names no tenant
    # (ADR-012 point 3). Empty on purpose, and declared so in
    # SINGLE_DIALECT_MIGRATIONS.
    # The runner still records the version, so both histories keep one
    # numbering.
    (10, []),
    (
        11,
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
    (
        12,
        [
            # The application profile is a track's own, like its framing
            # (spec 101). The only one that can exist is the default track's.
            """
            UPDATE profile_documents SET track_id = 1
            WHERE kind = 'application_profile' AND track_id IS NULL
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

# --- Owners and the row policy (spec 105, ADR-013 decision 1) ---
#
# Hosted only. Every table that holds personal data carries an owner_id, and
# a forced row policy confines a session to its owner's rows. These
# declarations are the "marked hosted" part of the one definition that
# ADR-013 decision 5 asks for. `tests/test_owner_policy.py` holds a migrated
# store to them, and `tests/test_dialect_parity.py` reads them to tell a
# deliberate difference from drift.

# Every table with an owner. The order is the order migration 10 looks for
# ownerless rows: a table before the table it references, so a refusal names
# the table that holds the row rather than its parent.
OWNED_TABLES: tuple[str, ...] = (
    "job_events",
    "jobs",
    "contacts",
    "profile_documents",
    "user_config",
    "job_runs",
    "tracks",
)

# Every other table, with the reason it has no owner.
UNOWNED_TABLES: dict[str, str] = {
    "schema_version": "migration bookkeeping, no personal data",
}

# Columns only the hosted store has, as (table, column).
HOSTED_ONLY_COLUMNS: tuple[tuple[str, str], ...] = tuple(
    (table, "owner_id") for table in OWNED_TABLES
)

# Keys that are per owner on Postgres and global on SQLite. Each maps (table,
# the key's columns on Postgres) to the same key on SQLite: the Postgres
# columns without the leading owner_id, or None for a key only Postgres has.
# A key here is a primary key, a unique constraint or a unique index.
OWNER_SCOPED_KEYS: dict[tuple[str, tuple[str, ...]], tuple[str, ...] | None] = {
    ("jobs", ("owner_id", "url")): ("url",),
    ("jobs", ("owner_id", "external_key")): ("external_key",),
    ("tracks", ("owner_id", "slug")): ("slug",),
    ("user_config", ("owner_id", "kind")): ("kind",),
    ("profile_documents", ("owner_id", "kind", "name")): ("kind", "name"),
    ("profile_documents", ("owner_id", "track_id", "kind", "name")): ("track_id", "kind", "name"),
    ("job_runs", ("owner_id", "job")): ("job",),
    ("tracks", ("owner_id", "id")): ("id",),
    # What job_events references, so an event stays inside its job's owner.
    # SQLite has no owner to stay inside.
    ("jobs", ("owner_id", "id")): None,
}

Dialect = Literal["sqlite", "postgres"]

# A version that applies to one dialect only: the dialect, and why. The other
# dialect's list holds the version with no statements, on purpose, so both
# runners record it and the two histories keep one numbering.
# `undeclared_dialects` accepts that empty list only for a version declared
# here with a reason (spec 105).
SINGLE_DIALECT_MIGRATIONS: dict[int, tuple[Dialect, str]] = {
    10: (
        "postgres",
        "owner_id and row-level policy (spec 105); "
        "the local schema names no tenant (ADR-012 point 3)",
    ),
}

MIGRATION_10_NEEDS_AUTH = (
    "migration 10 needs Supabase's auth schema (auth.users, auth.uid()) and the "
    "authenticated role; this store has none"
)

# The one role the owner policy serves and the one that holds harrier's
# table privileges. A request's transaction switches to it (spec 104).
# Supabase's Data API switches to `authenticated` instead, which holds
# nothing here, so it can never reach these tables and harrier.tracker
# stays the one write path (ADR-003), whatever schemas the project exposes.
TENANT_ROLE = "harrier_tenant"

# Every role a revoke names. Supabase creates anon, authenticated and
# service_role; the tests create them in a shim (tests/pg_support.py).
_EVERY_GRANTEE = f"PUBLIC, anon, authenticated, service_role, {TENANT_ROLE}"
_OWNER_IS_SESSION = "owner_id = (SELECT auth.uid())"
# Migration 8's seed (MIGRATIONS above): every owner's first track.
_FIRST_TRACK = "1, 'job', 'industry', 'Job search'"
# The identity sequences that stay global (spec 105, Honest limitations).
# tracks loses its own when its id stops being an identity.
_GLOBAL_ID_SEQUENCES = ", ".join(
    f"{table}_id_seq"
    for table in ("jobs", "contacts", "profile_documents", "user_config", "job_events")
)


def _sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _refuse_without_auth() -> str:
    # anon and service_role are checked too, because the revokes below name
    # them and would fail on a store without them.
    roles = " OR ".join(
        f"NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}')"
        for role in ("authenticated", "anon", "service_role")
    )
    return f"""
    DO $$
    BEGIN
        IF to_regclass('auth.users') IS NULL OR to_regprocedure('auth.uid()') IS NULL
            OR {roles}
        THEN
            RAISE EXCEPTION {_sql_text(MIGRATION_10_NEEDS_AUTH)};
        END IF;
    END
    $$
    """


def _create_tenant_role() -> str:
    # A role belongs to the whole server, not to this database, so another
    # store on the same server may have made it already, or be making it
    # now. NOINHERIT: it gains nothing from any role it is later made a
    # member of. NOBYPASSRLS: the policy always applies to it.
    return f"""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{TENANT_ROLE}') THEN
            CREATE ROLE {TENANT_ROLE} NOLOGIN NOINHERIT NOBYPASSRLS;
        END IF;
    EXCEPTION WHEN duplicate_object OR unique_violation THEN
        NULL;
    END
    $$
    """


def _refuse_ownerless_rows() -> str:
    checks = "\n".join(
        f"IF EXISTS (SELECT 1 FROM {table}{' WHERE id <> 1' if table == 'tracks' else ''}) "
        f"THEN RAISE EXCEPTION "
        f"{_sql_text(f'migration 10 found rows in {table} with no owner; it cannot choose one')};"
        " END IF;"
        for table in OWNED_TABLES
    )
    return f"DO $$\nBEGIN\n{checks}\nEND\n$$"


POSTGRES_OWNERS: list[str] = [
    # Both prechecks raise, which rolls the whole migration back and leaves
    # the store at version 9. No owner is ever guessed.
    _refuse_without_auth(),
    _refuse_ownerless_rows(),
    _create_tenant_role(),
    # The baseline's seeded track has no owner. Each owner gets their own
    # track 1 below instead.
    "ALTER TABLE tracks DISABLE TRIGGER tracks_are_never_deleted",
    "DELETE FROM tracks WHERE id = 1",
    "ALTER TABLE tracks ENABLE TRIGGER tracks_are_never_deleted",
    # No ON DELETE action: deleting an auth user who still owns rows is
    # refused. Spec 111 decides deletion.
    *(
        f"ALTER TABLE {table} ADD COLUMN owner_id uuid NOT NULL DEFAULT auth.uid() "
        "REFERENCES auth.users (id)"
        for table in OWNED_TABLES
    ),
    # The two references are rebuilt to carry the owner, so a row can only
    # name a row of its own owner. Dropped first: each depends on the key it
    # names.
    "ALTER TABLE job_events DROP CONSTRAINT job_events_job_id_fkey",
    "ALTER TABLE jobs DROP CONSTRAINT jobs_track_id_fkey",
    # Every per-owner key leads with owner_id, so it is also the index the
    # policy's filter uses.
    "DROP INDEX idx_jobs_url",
    "CREATE UNIQUE INDEX idx_jobs_url ON jobs(owner_id, url) WHERE url <> ''",
    "DROP INDEX idx_jobs_external_key",
    (
        "CREATE UNIQUE INDEX idx_jobs_external_key ON jobs(owner_id, external_key) "
        "WHERE external_key <> ''"
    ),
    "ALTER TABLE tracks DROP CONSTRAINT tracks_slug_key",
    "ALTER TABLE tracks ADD UNIQUE (owner_id, slug)",
    "ALTER TABLE user_config DROP CONSTRAINT user_config_kind_key",
    "ALTER TABLE user_config ADD UNIQUE (owner_id, kind)",
    "ALTER TABLE profile_documents DROP CONSTRAINT profile_documents_kind_name_key",
    "ALTER TABLE profile_documents ADD UNIQUE (owner_id, kind, name)",
    "ALTER TABLE job_runs DROP CONSTRAINT job_runs_pkey",
    "ALTER TABLE job_runs ADD PRIMARY KEY (owner_id, job)",
    # Track ids are per owner, so every owner's default track is 1, as it is
    # locally, and DEFAULT_TRACK_ID keeps its meaning under the policy.
    "ALTER TABLE tracks ALTER COLUMN id DROP IDENTITY",
    "ALTER TABLE tracks DROP CONSTRAINT tracks_pkey",
    "ALTER TABLE tracks ADD PRIMARY KEY (owner_id, id)",
    "ALTER TABLE jobs ADD UNIQUE (owner_id, id)",
    "ALTER TABLE jobs ADD FOREIGN KEY (owner_id, track_id) REFERENCES tracks (owner_id, id)",
    "ALTER TABLE job_events ADD FOREIGN KEY (owner_id, job_id) REFERENCES jobs (owner_id, id)",
    "CREATE INDEX idx_contacts_owner ON contacts(owner_id)",
    "CREATE INDEX idx_job_events_owner ON job_events(owner_id)",
    # A track inserted without an id gets one more than its owner's highest.
    # The lock is per owner and held to the end of the transaction, so two
    # tracks created at once by one owner get two ids. The two-key lock form
    # never meets the runner's one-key MIGRATION_LOCK_KEY. Qualified names
    # throughout: harrier_new_owner() fires this with an empty search_path.
    """
    CREATE FUNCTION harrier_number_track() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.id IS NULL THEN
            PERFORM pg_catalog.pg_advisory_xact_lock(
                pg_catalog.hashtext('harrier.tracks'),
                pg_catalog.hashtext(NEW.owner_id::text)
            );
            SELECT coalesce(max(id), 0) + 1 INTO NEW.id
            FROM public.tracks WHERE owner_id = NEW.owner_id;
        END IF;
        RETURN NEW;
    END
    $$
    """,
    """
    CREATE TRIGGER tracks_number_per_owner BEFORE INSERT ON tracks
    FOR EACH ROW EXECUTE FUNCTION harrier_number_track()
    """,
    # The baseline's refusals are row triggers, which TRUNCATE never fires,
    # so a role with TRUNCATE could empty either table, and TRUNCATE tracks
    # CASCADE would take every job with it. These fire for the superuser
    # too. SQLite has no TRUNCATE, so nothing there needs them.
    """
    CREATE TRIGGER job_events_append_only_truncate BEFORE TRUNCATE ON job_events
    FOR EACH STATEMENT EXECUTE FUNCTION harrier_refuse('job_events is append-only')
    """,
    """
    CREATE TRIGGER tracks_are_never_truncated BEFORE TRUNCATE ON tracks
    FOR EACH STATEMENT EXECUTE FUNCTION harrier_refuse('tracks are archived, never deleted')
    """,
    # Every new auth user gets track 1, whoever creates the user. A failure
    # here fails the sign-up, which is the honest result: an owner without
    # track 1 cannot use the product. SECURITY DEFINER with an empty
    # search_path, as Supabase documents for this pattern.
    f"""
    CREATE FUNCTION harrier_new_owner() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
    BEGIN
        INSERT INTO public.tracks (owner_id, id, slug, kind, label)
        VALUES (NEW.id, {_FIRST_TRACK});
        RETURN NEW;
    END
    $$
    """,
    """
    CREATE TRIGGER harrier_new_owner AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION public.harrier_new_owner()
    """,
    f"""
    INSERT INTO tracks (owner_id, id, slug, kind, label)
    SELECT id, {_FIRST_TRACK} FROM auth.users
    """,
    # One policy for every command (ADR-013 decision 1). USING decides what a
    # read, update or delete sees; WITH CHECK what an insert or update
    # stores. Without claims auth.uid() is null, so nothing matches. FORCE
    # subjects the table owner, but not a superuser or a BYPASSRLS role.
    *(
        statement
        for table in OWNED_TABLES
        for statement in (
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
            f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
            f"CREATE POLICY owner_only ON {table} AS PERMISSIVE FOR ALL TO {TENANT_ROLE} "
            f"USING ({_OWNER_IS_SESSION}) WITH CHECK ({_OWNER_IS_SESSION})",
        )
    ),
    # Row security on every table, so no table is the exception.
    "ALTER TABLE schema_version ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE schema_version FORCE ROW LEVEL SECURITY",
    f"CREATE POLICY version_readable ON schema_version FOR SELECT TO {TENANT_ROLE} USING (true)",
    # Privileges are stated, never inherited from a default: Supabase's
    # default for new tables in public has changed once already. Only the
    # tenant role gets any. anon and authenticated are the Data API's roles
    # and get none. service_role bypasses row security, so a table privilege
    # is all that limits it, and it gets none here. A trigger fires without
    # EXECUTE on its function, so revoking it takes nothing from a write.
    # No grant includes TRUNCATE.
    (f"REVOKE ALL ON {', '.join(OWNED_TABLES)}, {', '.join(UNOWNED_TABLES)} FROM {_EVERY_GRANTEE}"),
    f"REVOKE ALL ON SEQUENCE {_GLOBAL_ID_SEQUENCES} FROM {_EVERY_GRANTEE}",
    (
        "REVOKE ALL ON FUNCTION harrier_refuse(), harrier_number_track(), harrier_new_owner() "
        f"FROM {_EVERY_GRANTEE}"
    ),
    (
        "GRANT SELECT, INSERT, UPDATE, DELETE "
        f"ON jobs, contacts, profile_documents, user_config, job_runs TO {TENANT_ROLE}"
    ),
    # The append-only triggers refuse the rest on job_events, and
    # tracks_are_never_deleted on tracks.
    f"GRANT SELECT, INSERT ON job_events TO {TENANT_ROLE}",
    f"GRANT SELECT, INSERT, UPDATE ON tracks TO {TENANT_ROLE}",
    f"GRANT SELECT ON schema_version TO {TENANT_ROLE}",
    # The policy calls auth.uid() as the session's role, so the tenant role
    # must reach it. Supabase grants this to its own roles; this one is
    # harrier's.
    f"GRANT USAGE ON SCHEMA auth TO {TENANT_ROLE}",
    f"GRANT EXECUTE ON FUNCTION auth.uid() TO {TENANT_ROLE}",
]

# Spec 099: SQLite's migration 11, without its rebuild, and per owner. A
# track is (owner_id, id) since migration 10, so the reference carries the
# owner, and each partial index leads with owner_id as every per-owner key
# does. A shared row's NULL track_id leaves the reference unchecked.
POSTGRES_TRACK_OWNED_DOCUMENTS: list[str] = [
    "ALTER TABLE profile_documents ADD COLUMN track_id bigint",
    (
        "ALTER TABLE profile_documents ADD FOREIGN KEY (owner_id, track_id) "
        "REFERENCES tracks (owner_id, id)"
    ),
    "ALTER TABLE profile_documents DROP CONSTRAINT profile_documents_owner_id_kind_name_key",
    "UPDATE profile_documents SET track_id = 1 WHERE kind = 'resume_framing'",
    """
    CREATE UNIQUE INDEX idx_profile_documents_shared
    ON profile_documents (owner_id, kind, name) WHERE track_id IS NULL
    """,
    """
    CREATE UNIQUE INDEX idx_profile_documents_owned
    ON profile_documents (owner_id, track_id, kind, name) WHERE track_id IS NOT NULL
    """,
    # The dropped constraint was the table's whole-table index on owner_id,
    # which the policy's filter uses; the partial indexes cover only part of
    # the table each (spec 105).
    "CREATE INDEX idx_profile_documents_owner ON profile_documents(owner_id)",
]

POSTGRES_MIGRATIONS: list[tuple[int, list[str]]] = [
    (POSTGRES_BASELINE_VERSION, POSTGRES_BASELINE),
    (10, POSTGRES_OWNERS),
    (11, POSTGRES_TRACK_OWNED_DOCUMENTS),
    # Spec 101: SQLite's migration 12. Every owner has a track 1 (migration 10).
    (
        12,
        [
            "UPDATE profile_documents SET track_id = 1 "
            "WHERE kind = 'application_profile' AND track_id IS NULL",
        ],
    ),
]


def undeclared_dialects(
    sqlite: list[tuple[int, list[str]]],
    postgres: list[tuple[int, list[str]]],
    baseline: int = POSTGRES_BASELINE_VERSION,
    single_dialect: dict[int, tuple[Dialect, str]] | None = None,
) -> list[str]:
    """Every version after the baseline that one dialect lacks or leaves empty.

    A migration that lands for one store only makes the two drift apart in a
    way the parity test may not see, so each later version must appear in
    both lists with at least one statement (spec 103). The one exception is
    a version declared in `single_dialect` (SINGLE_DIALECT_MIGRATIONS by
    default) with a reason: the other dialect's list holds it empty on
    purpose, and must keep it empty (spec 105). Either dialect may be the
    one; neither may be empty without a declaration.
    """
    declared = SINGLE_DIALECT_MIGRATIONS if single_dialect is None else single_dialect
    lists: dict[Dialect, dict[int, list[str]]] = {
        "sqlite": {version: statements for version, statements in sqlite if version > baseline},
        "postgres": {version: statements for version, statements in postgres if version > baseline},
    }
    versions = set(lists["sqlite"]) | set(lists["postgres"])
    versions |= {version for version in declared if version > baseline}
    problems: list[str] = []
    for version in sorted(versions):
        only = declared.get(version)
        for dialect in ("sqlite", "postgres"):
            statements = lists[dialect].get(version)
            if only is not None and only[0] != dialect and statements is not None:
                if statements:
                    problems.append(
                        f"migration {version} is {only[0]} only but declares {dialect} statements"
                    )
            elif not statements:
                problems.append(f"migration {version} declares no {dialect} statements")
        if only is not None and not only[1].strip():
            problems.append(f"migration {version} is {only[0]} only without a reason")
    return problems
