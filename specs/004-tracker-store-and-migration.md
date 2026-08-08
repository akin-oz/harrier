---
spec: 004
title: Tracker store, profile tables, and legacy migration
status: in-progress
approved: yes
milestone: M1
depends: [002, 003]
---

# Spec 004: Tracker store, profile tables, and legacy migration

Refined from the stub before implementation; scope below is the real scope.

## Problem

The tracker becomes SQLite as truth (ADR-003) and all personal data moves into
the database (ADR-008). The old CSVs must migrate with full fidelity, including
the key=value store hiding in the notes column
(`~/job-hunt-local/scripts/job_sources.py`, extract_note_value).

## Scope

### Store

- SQLite at `data/tracker.db` (override: `HARRIER_DATA_DIR`), WAL mode,
  foreign keys on. Schema versioning via a `schema_version` table; migrations
  are forward-only functions in `harrier.tracker.schema`.
- `jobs` table: the 20 legacy columns
  (company, title, location, url, source, added_at, fit_score, status,
  applied_date, last_contact, next_action, outreach_status, last_outreach_at,
  next_outreach_action, best_contact_name, best_contact_linkedin,
  contacts_found, outreach_priority, rejection_reason, notes)
  plus columns promoted out of notes
  (score, archetype, source_label, external_key, signals, remote_filter,
  manual_reject, manual_added) plus id, created_at, updated_at.
  `status` has a CHECK constraint over the six lifecycle values.
- `contacts` table: the 17 legacy columns plus id, created_at, updated_at.
  linked_jobs stays serialized for parity; normalizing it is a later spec.
- Dedupe indexes: unique on non-empty url, unique on non-empty external_key
  (partial indexes), non-unique on (company, title). Part of the schema from
  version 1; the migration pre-scans the source for duplicates and aborts
  before touching the database, so the indexes can never fail mid-import.
- `profile_documents` table (ADR-008): kind, name, format, content,
  updated_at; unique (kind, name). Holds candidate profile, resume truth
  sources, achievements, application profile, outreach defaults, interview
  prep, story bank as documents. Structured profile schemas come with the
  specs that consume them (013+); this spec stores and round-trips.

### Write path

- `harrier.tracker.store` is the only module that opens the database for
  writing. It exposes: add_job (with url/external_key/company+title dedupe),
  set_status (validates the target status, stamps next_action defaults,
  applied seeds the outreach block exactly as
  `~/job-hunt-local/scripts/jobs.py:394` does), update_fields, list/get.
- Legal statuses: prospect, shortlisted, tailored_cv_requested, applied,
  interviewing, rejected. The old CLI sets any status from any status; parity
  keeps that liberal, but every change goes through set_status and is
  validated against the legal set.

### Migration

- `harrier migrate-legacy --jobs <csv> --contacts <csv>`: imports every row
  verbatim, expands notes key=value pairs into the promoted columns while
  preserving the original notes text, and prints a report
  (rows read, rows imported, notes keys expanded, duplicates found).
- Fidelity rule: the migration never drops, merges, or rewrites a row. If the
  duplicate scan finds url or external_key duplicates, it aborts with the
  duplicate list and imports nothing; resolution is a human edit of the
  source CSV, not a silent merge.
- Idempotence: running against an already-migrated database aborts unless
  --replace is given (drops and reimports tracker tables only).

### Profile import and export

- `harrier profile import --from <old-repo-root>`: reads the old repo's
  config files read-only (candidate.json, resume-candidate-data.json,
  resume-truth-source.md, latest-project-achievements.md,
  application-profile.md/.json, outreach/defaults.json, interview-prep/*)
  into profile_documents. Missing files are reported and skipped.
- `harrier profile export --to <dir>`: writes documents back out with their
  original names and formats.

### Export

- `harrier export`: writes tracker/jobs.csv and tracker/contacts.csv in the
  exact legacy 20-column and 17-column header order, to the (gitignored)
  tracker/ directory. `just export` calls it.

## Acceptance criteria

- [ ] Migration against synthetic fixtures mirroring the legacy shapes
      (including multiline quoted notes) asserts row counts and per-field
      fidelity against the source
- [ ] Migration against the real CSVs (local run, read-only source) imports
      every record; reported counts equal the source record counts
- [ ] An illegal status value raises; applied seeds the outreach block
- [ ] Duplicate url in source aborts the migration with the duplicate list
- [ ] Export then reimport round-trips
- [ ] Profile import from the old repo followed by export reproduces the
      source documents byte-identically
- [ ] All gates green; this PR proves the spec-gate check (flips spec 002)

## Proof / origin

scripts/job_sources.py TRACKER_FIELDS and extract_note_value; scripts/jobs.py:43
(statuses), :99 (repair), :394 (applied seeding); scripts/outreach_lib.py
CONTACT_FIELDS; docs/adr/ADR-003-tracker-store.md;
docs/adr/ADR-008-personal-data-in-database.md.

## Out of scope

The API layer (spec 005), the run manager (006), screening (007), structured
profile schemas and their validation (013+), normalizing linked_jobs, and any
write path for outreach state machines (016).
