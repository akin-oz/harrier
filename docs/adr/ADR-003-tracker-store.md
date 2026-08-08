# ADR-003: Tracker store

- Status: accepted
- Date: 2026-08-08

## Context

Today `tracker/jobs.csv` is the single source of truth (701 records, 20 columns), with
`tracker/contacts.csv` beside it (146 records). Writers today are already plural:
scheduled imports four times daily, the CLI, the GUI (which shells out to the CLI), the
capture server, and one automated mutator (`scripts/evaluate_prospects.py` auto-reject).
The old repo carries the scars of concurrent-ish CSV writing: three ad hoc backup files
(`tracker/jobs.csv.bak-*`), a `repair_tracker_row()` function that backfills drifted
columns (`scripts/jobs.py:99`), a schema triplicated across three files, and a `notes`
column that hides a key=value store (`score=`, `external_key=`, `signals=`) that dedupe
depends on (`scripts/job_sources.py`, extract_note_value).

The new world makes concurrency worse, not better: a long-running FastAPI process, the
CLI, and launchd runs all mutate state, sometimes simultaneously.

## Options

### CSV stays truth

Pros: greppable, diffable, zero migration, Time Machine-friendly, matches the file-based
ethos. Cons: no transactions and no locking story across three writer processes (the
current code has none; correctness has been luck and low write frequency); no schema
enforcement, which is how `repair_tracker_row` came to exist; the notes-column schema
stays hidden or requires inventing columns-in-CSV discipline by hand; every status
transition rule must be enforced in application code in every writer.

### SQLite as truth, CSV as export (recommended)

Pros: real transactions with WAL mode serving multiple processes on one machine, which
is exactly SQLite's design case; one schema definition with migrations; the notes
key=value data becomes real columns; status transitions enforced by one write path plus
CHECK constraints as a backstop; still a single file, still local-first, still trivially
backed up. Cons: not directly greppable; needs an export for eyeballing; migration cost
from the two CSVs.

### Third option considered: JSONL event log with materialized state

Append-only events with derived state gives audit history but reinvents a database
poorly, and every reader needs the materializer. Rejected as complexity without a user.

## Decision

SQLite is the source of truth: one file, `data/tracker.db`, WAL mode. The write path is
exactly one Python module in the domain package; the API, the CLI, and scheduled runs
all call it. Nothing else opens the database for writing.

CSV keeps two roles it is genuinely good at:

1. **Export**: `harrier export` writes `tracker/jobs.csv` and `tracker/contacts.csv` in
   the current 20-column and 17-column shapes, and every mutating CLI command touches
   the export afterward, so grep, diff, and backup workflows survive.
2. **Import**: the one-shot migration reads the old repo's CSVs, expands the notes
   key=value pairs into columns, and preserves the original `notes` free text.

The status lifecycle (`prospect`, `shortlisted`, `tailored_cv_requested`, `applied`,
`interviewing`, `rejected`) and the orthogonal outreach axis carry over unchanged;
transitions live in the write path and changing them requires a spec.

## Consequences

- Concurrent writer correctness stops depending on luck. WAL allows readers during
  writes; writes serialize.
- The dedupe indexes (url, company+title, external_key) become actual unique indexes
  instead of in-memory sets rebuilt per run.
- Greppability is one `just export` away rather than free; the README states this
  tradeoff honestly.
- The tracker file is classified never-in-git (ADR-002); backup is local file copy plus
  the CSV exports.
- Migration is a walking-skeleton milestone task with a row-count and field-fidelity
  assertion against the source CSVs.
