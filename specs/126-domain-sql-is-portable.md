---
spec: 126
title: Domain SQL is portable
status: accepted
approved: yes
milestone: M10
depends: [079, 099, 103, 105, 112, 115, 122]
---

# Spec 126: Domain SQL is portable

Split from spec 112 on 2026-10-11 (spec 112, Amendment (2026-10-11):
split, owner and nesting, decision 1). This is part B. Spec 112 is the
seam (A); spec 127 is the CLI and API gates (C); spec 128 is three review
findings (D).

## Problem

After spec 112's second pull request, a `Connection` speaks both dialects:
it rewrites `?` placeholders, maps driver errors to store errors, nests
transactions as savepoints, and acts as one owner under the row policy.
The domain's SQL on top of it still speaks SQLite. Line numbers below are
from `main` at `87c3926` on 2026-10-11, refreshed from spec 112's Problem,
which counted them on the spec 103 branch.

- **Timestamps from the database's SQLite clock.** `datetime('now')` is
  SQLite's; Postgres has no such function. Nine sites:
  `tracker/store.py:448` and `:509`; `tracks.py:414`;
  `userconfig/store.py:171` and `:174`; `profile/store.py:44`, `:48`,
  `:52` and `:56`.
- **Case folding in the dedupe.** `COLLATE NOCASE` at
  `tracker/store.py:111` and `:116`. Postgres has no `NOCASE` collation.
  SQLite's `NOCASE` folds the 26 ASCII letters only; Postgres `lower()`
  follows the database locale and folds `É` too.
- **Text ordering.** `ORDER BY` over text at `userconfig/store.py:209`,
  `profile/store.py:129` and `:140`, `apply/profile.py:49` and `:60`,
  `resume/content.py:765` and `resume/split.py:46`. SQLite orders text by
  bytes; Postgres by the database's collation. Two of these take
  `LIMIT 1` (`apply/profile.py:49`, `resume/content.py:765`), so a
  different document can be chosen.
- **New ids.** `cursor.lastrowid` at `tracker/store.py:208` (`add_job`) and
  `:923` (`add_contact`). psycopg has no `lastrowid`.
- **Four upserts whose conflict targets Postgres cannot match.**
  `runoutcome.py:146-152` (`ON CONFLICT(job)`), `userconfig/store.py:168-177`
  (`ON CONFLICT (kind)`), and two in `profile/store.py`: `_UPSERT_SHARED`
  (`:42-49`, `ON CONFLICT (kind, name) WHERE track_id IS NULL`) and
  `_UPSERT_OWNED` (`:50-57`, `ON CONFLICT (track_id, kind, name) WHERE
  track_id IS NOT NULL`). Spec 112 named three; spec 099 split the
  profile upsert into two, each naming one partial index of migration 11
  (`tracker/schema.py:466-471`). On Postgres spec 105 made every one of
  these keys per owner: `(owner_id, job)`, `(owner_id, kind)` and the two
  partial indexes over `(owner_id, kind, name)` and `(owner_id, track_id,
  kind, name)` (`tracker/schema.py:931`, `:935`, `:1067-1072`). Postgres
  refuses a conflict target that matches no unique index, and naming
  `owner_id` in domain SQL would make it name a tenant (ADR-013
  decision 2).
- **The write lock covers the whole store.** `_write_lock`
  (`tracker/store.py:309-330`, after spec 112 `transaction(immediate=True)`)
  takes SQLite's file-wide write lock for four writers: `set_status`
  (`:379`), `update_fields` (`:495`), `backfill_events` (`:685`) and spec
  122's `record_eligibility` (`:850`). Postgres has no `BEGIN IMMEDIATE`,
  and a store-wide lock there would make every owner wait for every other.
  Spec 112 named three writers; spec 122 added the fourth.
- **Local-only work takes any connection.** `cutover.preflight`
  (`cutover.py:145`), `cutover.run_cutover` (`:430`),
  `cutover.tracker_check` (`:110`), `cutover.verify` (`:297`) and
  `tracker.migrate_legacy.migrate` (`migrate_legacy.py:91`) move the old
  system onto the local tracker file. Given a Postgres connection they
  would write the old system's rows into a hosted owner's store.
- **The domain's tests run on SQLite only.** The tests that hold the
  domain's behavior open `harrier.db.connect`. Nothing proves the domain
  on Postgres, under the row policy it will run under.
- **The docs say the domain does not run on Postgres.**
  `docs/architecture.md` says "Spec 112 ports the domain's reads and
  writes to it" and "the domain does not run on Postgres until spec 112".

## Scope

One spec, two pull requests in order: B1 changes the SQL and proves each
change on both stores; B2 moves the domain's test files onto the
dual-store fixture B1 builds. B1 is the behavior; B2 changes no code under
`services/api/src`.

B1:

1. **One timestamp helper.** `harrier.connection.store_now()` returns the
   current UTC time as `YYYY-MM-DD HH:MM:SS`, the text `datetime('now')`
   writes today. The nine sites under Problem bind it as a parameter.
2. **ASCII case folding on both stores** in `find_duplicate`
   (`tracker/store.py:111`, `:116`), through a seam helper (Behavior).
3. **Code point ordering on both stores** at the seven `ORDER BY` sites
   under Problem, through a seam helper (Behavior).
4. **New ids from `RETURNING id`** in `add_job` and `add_contact`.
   `lastrowid` leaves the `Cursor` protocol (`connection.py:121-122`).
5. **The four upserts become update-then-insert** (Behavior), one text
   per upsert for both dialects, naming no tenant.
6. **The write lock per store.** SQLite keeps `BEGIN IMMEDIATE`. Postgres
   locks the job rows a write depends on (Behavior).
7. **Local-only functions refuse a Postgres connection** with
   `StoreLocalOnlyError`, a new `StoreError` subclass in
   `harrier.connection`.
8. **The dual-store fixture** (Behavior), built on spec 112's
   `open_store(owner=...)`, in `services/api/tests/conftest.py` and
   `services/api/tests/pg_support.py`. No new file.
9. **Targeted tests on both stores** for items 1 to 7, and the four lock
   tests (Behavior).
10. **Docs.** `docs/architecture.md`'s hosted section says the domain runs
    on either store, names `services/api/src/harrier/connection.py` and
    the fixture's tests, and stops saying "until spec 112". It says the
    CLI and the API still refuse a Postgres URL until specs 127 and 104.
    `specs/103-the-tracker-store-speaks-postgres.md` is not edited.

B2:

11. **The domain's test files run on both stores** through the fixture:
    the seven files of spec 112's table, `test_runs.py`'s `job_runs`
    tests, and `test_eligibility.py` (Open decision 1).

## Behavior

**Local, URL unset.** Every command, the API and the demo behave as
today. Every value read through the API keeps its shape and text.
Timestamps keep their format.

**Timestamps.** Every timestamp the domain writes is `store_now()`, bound
as a parameter. Column defaults stay as spec 103 set them, so an insert
that omits a timestamp gets the database's clock in the same format.

**Case folding in the dedupe.** On both stores, `find_duplicate` treats
company and title as equal when they are equal after folding `A` to `Z`
to `a` to `z`, and only those. A seam helper,
`harrier.connection.ascii_folded(conn, expression)`, returns the fragment
for the connection's dialect: on SQLite the expression with `COLLATE
NOCASE`, on Postgres `translate(<expression>, 'ABC...XYZ', 'abc...xyz')`.
The statement is written once, with the fragment in it.

| company in tracker | company added | duplicate on SQLite | on Postgres |
|---|---|---|---|
| `Acme` | `ACME` | yes | yes |
| `École Exemple` | `ÉCOLE EXEMPLE` | yes | yes |
| `École Exemple` | `école exemple` | no | no |

The `É` is the same character in the first two names, so only ASCII
letters differ; `É` and `é` are two characters, folded by neither store.
Corrected by the Amendment (2026-10-12): what B1 settled.

**Text ordering.** Every `ORDER BY` over text in domain SQL orders by code
point on both stores, through `harrier.connection.by_code_point(conn,
expression)`: the expression as written on SQLite, whose default is
binary, and with `COLLATE "C"` on Postgres. A NULL orders first on both:
SQLite's default for an ascending order, which Postgres takes from `NULLS
FIRST` written after the expression. `list_documents` is the one site
that orders by a column that can be NULL (`t.slug`, for a shared
document), so a shared document lists before an owned one of the same
kind and name on both stores (Amendment (2026-10-12): what B1 settled).

**New ids.** `add_job` and `add_contact` read the new id from `INSERT ...
RETURNING id`, inside the transaction that inserts, on both stores. Spec
112 refuses a SQLite library older than 3.35, the first with `RETURNING`.
No domain insert supplies an `id`: every new id comes from the store. An
explicit id would be refused on Postgres, where the tenant role may not
write `id` (spec 105, migration 13), and on another owner's row would
reveal that the id is taken.

**Upserts.** Each of the four becomes, in one `transaction()` on both
stores:

1. Update the row the key names, setting the new values and, where the
   table has one, `updated_at = store_now()`.
2. When the update changed no row, insert the row inside a nested
   `transaction()`, which spec 112 makes a savepoint.
3. When that insert raises `StoreIntegrityError(kind="unique")` (another
   writer inserted the key first), the savepoint rolls back and the update
   runs once more. When the second update changes a row, the upsert has
   succeeded. When it changes none, the integrity error is raised.

The savepoint is what lets step 3 run on Postgres, where an error aborts
the rest of a transaction unless it rolled back to a savepoint.

The `WHERE` clause of each update names the key and nothing else:

| Upsert | Update matches | Insert can conflict on |
|---|---|---|
| `runoutcome.record_success` | `job = ?` | SQLite `job` primary key; Postgres `PRIMARY KEY (owner_id, job)` |
| `userconfig.set_config` | `kind = ?` | SQLite `UNIQUE (kind)`; Postgres `UNIQUE (owner_id, kind)` |
| `profile.put_document`, shared | `kind = ? AND name = ? AND track_id IS NULL` | `idx_profile_documents_shared` |
| `profile.put_document`, owned | `track_id = ? AND kind = ? AND name = ?` | `idx_profile_documents_owned` |

**A NULL `track_id` is matched by `IS NULL`, written in the SQL text.**
The shared update never binds a `None` to `track_id = ?`. `NULL = NULL`
is not true on either store, so such an update would change no row; the
insert would then meet the shared partial index, and the retried update
would change no row again, so a write meant to replace a shared document
would fail with the integrity error instead. `track_id IS NULL` is the
predicate of the shared partial index on both stores
(`tracker/schema.py:466-467` for SQLite, `:1067-1068` for Postgres), so
the update matches exactly the rows that index holds unique. The owned
update binds an integer: `_upsert` refuses a `None` `track_id` for an
owned kind before it writes (`profile/store.py:63-64`), so the owned text
never sees one, and `track_id = ?` with an integer matches exactly the
rows of `idx_profile_documents_owned` (`track_id IS NOT NULL`).

On Postgres the update runs under the row policy and sees only the owner's
rows, and the insert meets the owner's own unique index, so no text names
`owner_id`. The owned write's `refuse_archived_track` check stays after
the write, inside its transaction, as today (`profile/store.py:71-74`).
`put_documents_and_rekind` (`:92-113`) keeps its one transaction; each
upsert inside it is a savepoint within it.

**The write lock.** The guarantee, on both stores: a decision on a job
(`set_status`), a field write (`update_fields`), an assessment
(`record_eligibility`) and a backfill (`backfill_events`) read the row,
its events, its assessments and the company's outcomes only after every
earlier writer of that job has committed, and a later writer of that job
reads what this one wrote.

- SQLite: `BEGIN IMMEDIATE`, unchanged. Every write in the file waits.
- Postgres: inside one transaction the job row is locked first
  (`SELECT id FROM jobs WHERE id = ? AND track_id = ? FOR UPDATE`), and
  every read the write depends on is a later statement. Postgres runs at
  read committed, so each later statement sees every transaction
  committed before it began. Writers of other jobs do not wait.
- Postgres backfill locks the track's jobs that have no events, in id
  order, then reads again, in a new statement, which of them still have
  none, and plans from that second read. This spec does not rely on what
  Postgres's re-check of a waited-for row sees in `job_events`.
- A job that does not exist, or is in another track or another owner's
  rows, locks nothing; the read that follows raises `JobNotFoundError` as
  today.
- `_write_lock` runs the Postgres lock when `conn.dialect` is
  `"postgres"`. It is the one place in domain code that reads the
  dialect.

**Local-only work on a Postgres connection.** The five functions under
Problem raise `StoreLocalOnlyError` before any statement runs:
`harrier cutover is local only: it moves the old system onto the local
tracker file`, and `harrier migrate-legacy is local only: it imports the
old system's CSVs into the local tracker file`.

**The dual-store fixture.** A pytest fixture, `store`, yields a migrated
`Connection` per test, parametrized `sqlite` and `postgres`.

- SQLite: `harrier.db.connect()` on a file under `tmp_path`, as the tests
  open it today.
- Postgres: once per session, a database created by `fresh_database`
  (`services/api/tests/pg_support.py:397-427`) and migrated with
  `apply_postgres_migrations`, as `harrier_migrator`. Per test, a copy
  (`CREATE DATABASE <copy> TEMPLATE <template>`), one synthetic owner
  inserted into `auth.users` with `new_owner` (which gives the owner
  track 1 through `harrier_new_owner()`), and
  `open_store({URL_VARIABLE: <copy URL>}, owner=<owner>)`. The copy is
  dropped after the test.
- The Postgres connection acts as the owner under the real row policy
  (spec 112, decision 2). Before yielding it, the fixture reads
  `current_user` in a transaction and fails the test unless it is
  `harrier_tenant`, for the reason `as_owner` gives
  (`services/api/tests/pg_support.py:315-325`): a lost role switch would
  run every test as the migrating role, which bypasses the policy.
- A test that needs a second owner creates one with `new_owner` through a
  migrator connection to the same copy and opens a second `open_store`.
- Postgres parameters skip locally without a test server and fail in CI
  without one (`pg_support._in_ci`), as spec 103 and spec 039 require.

**The targeted tests (B1).** Each change in items 1 to 7 has a test that
runs on both parameters (Acceptance criteria). The three lock tests of
spec 079 run on both:
`services/api/tests/test_job_events.py::test_a_first_move_holds_the_lock_from_read_to_write`,
`services/api/tests/test_job_events.py::test_backfill_holds_the_lock_from_read_to_write`,
`services/api/tests/test_job_events.py::test_a_field_update_holds_the_lock_from_read_to_write`,
and a fourth for `record_eligibility`. Their rival opens with
`busy_timeout_ms = 0` and records `waited` when it gets `StoreBusyError`.
On Postgres the rival is the same owner on a second connection.

**The test files (B2).** These move their domain tests onto `store`:

| File | Test functions on `main`, 2026-10-11 |
|---|---|
| `test_tracker_store.py` | 9 |
| `test_tracks.py` | 11 |
| `test_userconfig.py` | 49 |
| `test_profile_store.py` | 4 |
| `test_job_events.py` | 24 |
| `test_tracker_invariants.py` | 26 |
| `test_tracker_queries_name_their_track.py` | 3 |
| `test_eligibility.py` | 35 |

Counted with `grep -cE '^(async )?def test_'`, so a parametrized test
counts once. The `job_runs` tests move too: they are in
`test_run_outcome.py`, not in `test_runs.py`, whose one test of
`job_runs` is a migration test and stays SQLite (Amendment (2026-10-12):
what B2 settled). A test that reads SQLite itself stays SQLite only:
migration history read through raw `sqlite3.connect` (`test_tracks.py`),
`PRAGMA table_info` (`test_userconfig.py:73`). So does a test that drives
the CLI or the API, which refuse a Postgres URL until specs 127 and 104.
Each such test carries `@pytest.mark.sqlite_only(reason)`, registered in
`services/api/tests/conftest.py`, which gives a test that takes the
fixture its SQLite parameter alone. A test that opens no store, such as
one that reads source text or calls a pure function, runs once, as
today. A test that seeds rows with an explicit `id` changes to read the
id the store returned, because the tenant role may not write `id`. The
PR lists every SQLite-only test in these files with its reason.

## Failure modes

- **Two writers upsert the same key at once.** One inserts; the other's
  insert meets the unique index, rolls back to its savepoint and updates.
  One row, holding the later writer's values, on both stores.
- **The retried update changes no row.** The integrity error is raised
  and the transaction rolls back. This happens only when the row was
  deleted between the insert's refusal and the retry; no domain code
  deletes these rows today.
- **A shared document written with a `None` key in a hand-written test.**
  The `IS NULL` text matches it; there is no path that binds `None` to
  `track_id = ?`.
- **A writer waits past its timeout.** `StoreBusyError` on both stores,
  and the transaction rolls back. No retry.
- **A deadlock on Postgres.** `StoreDatabaseError`, the transaction rolls
  back. Every lock taken here is one row, or many in id order, so none is
  expected; none is retried.
- **A local-only function given a Postgres connection.**
  `StoreLocalOnlyError` before any statement runs.
- **The template database has another connection when a copy is made.**
  Postgres refuses `CREATE DATABASE ... TEMPLATE`; the test errors with
  the server's message. The fixture closes its own connection to the
  template before the first copy.
- **Postgres tests without a server.** Skipped locally, failed in CI
  (`CI=true`).
- **A domain insert that supplies an `id`.** Refused on Postgres, and
  `services/api/tests/test_domain_sql_on_both_stores.py::test_no_domain_insert_supplies_an_id` fails on both.

## Acceptance criteria

B1 (behavior):

- [ ] The four upserts are update-then-insert with one retry, pass on
      both stores, and two writers inserting the same key at once leave
      one row on both, for each of the four
      (`services/api/tests/test_domain_sql_on_both_stores.py::test_an_upsert_racing_another_leaves_one_row`).
- [ ] A shared document and an owned document of the same kind and name
      are written, rewritten and read back as two rows on both stores; a
      second write of the shared one replaces it rather than failing
      (`services/api/tests/test_domain_sql_on_both_stores.py::test_a_shared_and_an_owned_document_upsert_alike_on_both_stores`).
- [ ] `updated_at` written by `set_status`, `update_fields`,
      `set_config` and `put_document`, and `archived_at` written by
      `archive_track`, matches
      `^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$` on both
      (`services/api/tests/test_domain_sql_on_both_stores.py::test_a_written_timestamp_has_one_shape_on_both_stores`).
- [ ] The three rows of the folding table hold on both
      (`services/api/tests/test_domain_sql_on_both_stores.py::test_dedupe_folds_ascii_case_only_on_both_stores`).
- [ ] Text orders by code point on both, a NULL first, and `LIMIT 1` by
      name picks the same document
      (`services/api/tests/test_domain_sql_on_both_stores.py::test_text_orders_by_code_point_on_both_stores`).
- [ ] `add_job` and `add_contact` return the inserted id on both
      (`services/api/tests/test_domain_sql_on_both_stores.py::test_an_insert_returns_its_id_on_both_stores`).
- [ ] No domain insert supplies an `id`
      (`services/api/tests/test_domain_sql_on_both_stores.py::test_no_domain_insert_supplies_an_id`).
- [ ] A duplicate track slug is `DuplicateTrackError`, and a unique-index
      race in `add_job` is `DuplicateJobError`, on both
      (`services/api/tests/test_domain_sql_on_both_stores.py::test_a_duplicate_is_refused_alike_on_both_stores`).
- [ ] The three lock tests named under Behavior pass on both stores, and
      `record_eligibility` holds the lock from read to write on both
      (`services/api/tests/test_job_events.py::test_an_assessment_holds_the_lock_from_read_to_write`).
- [ ] A decision on one job does not wait for another on Postgres and
      waits on SQLite
      (`services/api/tests/test_job_events.py::test_a_decision_on_one_job_does_not_wait_for_another`).
- [ ] `cutover` and `migrate_legacy.migrate` refuse a Postgres connection
      with the texts under Behavior
      (`services/api/tests/test_domain_sql_on_both_stores.py::test_local_only_work_refuses_a_postgres_connection`).
- [ ] The fixture fails a Postgres test whose connection is not
      `harrier_tenant`
      (`services/api/tests/test_domain_sql_on_both_stores.py::test_the_store_fixture_runs_as_the_tenant`).
- [ ] `grep -n "datetime('now')\|COLLATE NOCASE\|lastrowid\|ON CONFLICT"`
      over `services/api/src/harrier`, outside `tracker/schema.py` and
      `db.py`, finds nothing but the one line of `ascii_folded` in
      `connection.py` that spells SQLite's fragment; the PR shows the
      output.
- [ ] `docs/architecture.md` describes the domain on either store, names
      `harrier/connection.py`, and no longer says "until spec 112".
- [ ] `services/api/src/harrier/tracker/schema.py` and
      `packages/contract` do not change.
- [ ] The CI log of `check-python` shows the Postgres parameters of the
      tests above passing, not skipped, and `just check` passes.

B2 (tests):

- [ ] Every test in the files under Behavior that opens a store and is
      not marked `sqlite_only` runs with both parameters, and the CI log
      of `check-python` shows the Postgres parameters passing, not
      skipped.
- [ ] No file under `services/api/src` changes in B2.
- [ ] `just check` passes.

## Honest limitations

- After this spec, nothing outside the tests reads or writes tracker rows
  on Postgres. The CLI runs only `harrier store` there until spec 127, and
  the API waits for spec 104. The proof is the suite run on both stores
  in CI.
- Locally the lock is still the whole file: a decision on one job waits
  for a decision on another. That is today's behavior and stays.
- The Postgres guarantee covers the four locked writers. Inserts
  (`add_job`, `add_contact`, `add_track`) and upserts rely on unique
  constraints on both stores, as SQLite's deferred transactions do today.
- Written timestamps come from the process's clock, defaults from the
  database's. Hosted, those are two machines; a skew between them can
  order a row's `created_at` after its `updated_at`.
- `set_status` and `add_job` hash the description cached under the
  process's `data/` directory (`_description_sha256`, `tracker/store.py:519`).
  On a hosted store that cache is spec 107's.
- ASCII-only folding keeps SQLite's rule on both stores. The screening
  dedupe folds with Python's `str.lower()` (`screening/normalized.py:37-38`),
  which folds `É` too. The two rules already disagree locally; this spec
  does not reconcile them.
- `_write_lock` reads `conn.dialect`. It is the one branch on dialect in
  domain code; the helpers in `harrier.connection` hold the others.
- CI time grows by the Postgres parameters and by one database copy per
  test. Not measured; each PR records `check-python`'s duration before and
  after.

## Migration

None. No schema change, no data change, no contract change. A local
tracker file opens exactly as before.

## Changes to approved specs

Each line below is a change this spec asks of an approved spec. None is
written into that spec here; each is an amendment Akin approves with this
spec.

- **Specs 104, 106, 107, 108, 109, 111, 116, 117 and 122:** each depends
  on spec 112 for the domain on Postgres, which is now this spec. Each
  `depends` gains 126.
- **Specs 108 and 116:** "spec 112's update-then-insert shape" is this
  spec's (Behavior, "Upserts").

## Options weighed

- **Two statement texts per dialect at each site.** Simple to read, and
  every later change must be made twice. Not chosen: the helpers keep one
  text.
- **`lower()` on both stores.** SQLite's built-in `lower()` folds ASCII
  only, Postgres's follows the locale, so the stores would disagree on
  accented names. Not chosen (spec 112, Open decision 3).
- **Column collations in the Postgres schema** (`COLLATE "C"` on text
  columns). Removes the ordering helper, and is a schema change, which is
  out of scope. Not chosen.
- **`INSERT ... ON CONFLICT ON CONSTRAINT <name>`.** Names a per-owner
  constraint, so the text would differ per dialect, and the shared and
  owned partial indexes are indexes, not constraints. Not chosen.
- **The test files moved in B1.** One pull request again near the size
  spec 112's analysis rejected. Not chosen: B2 changes tests only.

## Open decisions for Akin

1. **`test_eligibility.py` in B2.** Recommended: yes. Spec 122 added
   `job_eligibility` and a fourth locked writer after spec 112 named its
   seven files, and its tests are the domain's behavior on that table.
   Alternative: the seven files only, and `record_eligibility`'s lock
   test alone on both stores.
2. **The helpers' home.** Recommended: `harrier.connection`, beside the
   seam, so no domain module imports a dialect. Alternative: a new
   `harrier/sqltext.py`, which needs a classification entry first.
3. **The fixture's owner.** Recommended: one synthetic owner per test,
   created in the copy. Alternative: one owner baked into the template,
   which saves an insert per test and makes every test share the owner's
   id.

## Proof / origin

- `specs/112-the-tracker-domain-runs-on-either-store.md`: Problem,
  Behavior (Timestamps, Case folding, Text ordering, New ids, Upserts,
  The write lock, The tests on both stores, Local-only work), Open
  decisions 2 to 6 and 10, and the Amendment (2026-10-11): split, owner
  and nesting.
- `specs/105-every-personal-table-is-owned-and-policed.md`, Open decision
  1: update, then insert, naming no tenant.
- `specs/101-academic-application-documents-pass-the-gates.md` and
  `specs/099-an-academic-track-holds-its-own-framing.md`: shared and
  owned documents, and migration 11's partial indexes.
- `specs/079-decisions-are-recorded-as-events.md`, amendment of the review
  of the fixes: the write lock and its three tests.
- `specs/122-each-posting-says-whether-it-can-hire-from-turkiye.md`:
  `record_eligibility` under the write lock.
- `specs/039-gates-that-can-fail.md`: a gate that skips in CI has not run.
- `services/api/tests/pg_support.py`: `fresh_database`, `new_owner`,
  `as_owner`.
- `docs/adr/ADR-013-hosted-multi-tenant-deployment.md`, decision 2:
  domain SQL names no tenant.

## Out of scope

- The seam itself (`Connection`, `open_store`, the owner's identity,
  error mapping, savepoints): spec 112.
- The CLI's and the API's refusals on a Postgres URL: spec 127 and spec
  104.
- The import-linter contract, `scrub_secrets`, and `harrier store
  migrate` under its lock: spec 128.
- Native timestamp types, column collations, and any schema change.
- Files under `data/` on a hosted store: spec 107.
- Found while drafting, each for its own spec:
  - Screening dedupe folds Unicode case and the tracker folds ASCII only
    (spec 112, Out of scope, which still holds).
  - `add_job` maps every integrity refusal to `DuplicateJobError`
    (`tracker/store.py:203-207`), a check or unknown-track refusal
    included. Kept as is here so both stores behave the same.

## Amendment (2026-10-12): what B1 settled

Building B1 showed three places where this spec said something the
stores do not do, and one gap. Each is corrected above, in place, and
recorded here.

### Where the spec was wrong

- **The folding table had two answers swapped.** It said `École
  Exemple` and `ÉCOLE EXEMPLE` are not duplicates and `École Exemple` and
  `école exemple` are. The `É` is the same character in the first pair,
  and every other letter is ASCII, so SQLite's `NOCASE` matches them;
  `É` and `é` are two characters, and it does not match the second pair.
  Run against SQLite and Postgres 17 on 2026-10-12: `translate` agrees
  with `NOCASE` on all three rows, and `lower()` on Postgres matches the
  second pair, which is what the helper exists to prevent. The table now
  says so (`services/api/tests/test_domain_sql_on_both_stores.py::test_dedupe_folds_ascii_case_only_on_both_stores`).
- **The grep criterion could not find nothing.** `ascii_folded` is the
  seam's spelling of SQLite's fragment, so `COLLATE NOCASE` appears in
  `connection.py` once, on the line that returns it. The criterion now
  names that line as the one exception.
- **`archive_track` writes `archived_at`.** The timestamp criterion said
  `updated_at` for all five writers; `tracks` has no `updated_at`. The
  criterion now names the column `archive_track` writes.

### The gap: NULL ordering

Text ordering said nothing of NULLs. SQLite orders a NULL first in an
ascending order and Postgres last. `list_documents` orders by `t.slug`,
which is NULL for a shared document, so a shared document and an owned
one of the same kind and name listed in opposite orders on the two
stores. The site writes `NULLS FIRST`, SQLite's default, so both list the
shared one first (Behavior, Text ordering;
`services/api/tests/test_domain_sql_on_both_stores.py::test_text_orders_by_code_point_on_both_stores`).

### What the implementation settled

- **The helpers.** `store_now`, `ascii_folded`, `by_code_point`,
  `refuse_unless_local` and `update_or_insert` live in
  `services/api/src/harrier/connection.py` (Open decision 2's
  recommendation). `update_or_insert` is the four upserts' one shape:
  each passes its update and its insert, and a second writer's insert
  that wins the race is retried as an update in a savepoint.
- **The write lock.** `_write_lock(conn, scope, job_id)` in
  `services/api/src/harrier/tracker/store.py` replaces the four writers'
  `transaction(immediate=True)`. A `job_id` of None is backfill's lock,
  on the track's jobs with no events.
- **The fixture.** `store` yields the first `Connection`; `dual_store`,
  which `store` is built on, opens a second one as the same owner, opens
  one as a second owner, and gives the copy's URL as the migrating role.
  Its Postgres side lives in `services/api/tests/pg_support.py`
  (`migrated_template`, `database_copy`, `open_owner_store`). The
  targeted tests are in a new file,
  `services/api/tests/test_domain_sql_on_both_stores.py`, and the lock
  tests stay in `services/api/tests/test_job_events.py`.
- **A race on SQLite cannot reach the retry.** The first writer's update
  takes the write lock, so the second writer waits and then updates. The
  race test asserts one row, holding the later writer's value, on both
  stores, and each writer's updates and inserts in order: on Postgres the
  refused insert and the retried update; on SQLite that the rival met the
  held write lock and its update changed the writer's row, with no insert.
  Only Postgres reaches the retry, and reverting the retry or the
  savepoint fails its Postgres parameters only.
- **`lastrowid` in tests.** Two test helpers read it
  (`services/api/tests/test_job_events.py` and
  `services/api/tests/test_ui_operations.py`); with it gone from the
  `Cursor` protocol they read `RETURNING id`.
- **The wrapped first move.** Spec 112's
  `services/api/tests/test_job_events.py::test_a_wrapped_first_move_still_holds_the_lock_from_read_to_write`
  shares the rival fixture with the three lock tests, so it runs on both
  stores too.

### Honest limitations (amendment of 2026-10-12)

- `list_config` orders `user_config.kind` by code point on both stores,
  and no test can tell: the kinds harrier accepts are lowercase ASCII
  words that sort alike under either rule.
- The race test on SQLite proves one row, not the retry: SQLite cannot
  interleave two writers there.

## Amendment (2026-10-12): what B2 settled

Moving the test files showed two places where the B2 list named a file
for something it does not hold, and two gaps. Each is corrected above, in
place, and recorded here.

### Where the list was wrong

- **The `job_runs` tests are in `test_run_outcome.py`.** Spec 112's table
  named `test_runs.py`'s `job_runs` tests, and this spec copied it.
  `test_runs.py` tests the API's run manager and its journal, which open
  no store; its one test of `job_runs`,
  `services/api/tests/test_runs.py::test_a_database_already_at_the_previous_version_gains_the_job_runs_table`,
  reads migration history through raw `sqlite3.connect` and stays SQLite.
  The tests that write and read `job_runs` through the domain
  (`record_success`, `last_success`, the digest's schedule lines) are in
  `services/api/tests/test_run_outcome.py`, and they move.
- **`test_tracker_queries_name_their_track.py` has nothing to move.** Its
  three tests read the source text of `harrier`; none opens a store.

### The gaps

- **The CLI and the API stay SQLite.** The list gave one reason for a
  test to stay on SQLite. A test that drives `harrier_cli.main.main` or
  the API's `TestClient` stays too: both open the local store, and refuse
  a Postgres URL until specs 127 and 104 (Honest limitations).
- **The mark.** A SQLite-only test is marked
  `@pytest.mark.sqlite_only(reason)`. `services/api/tests/conftest.py`
  registers the mark, refuses one with no reason at collection, and gives
  a marked test that takes the dual-store fixture its SQLite parameter
  alone, so the CLI and the API meet the same file the test wrote
  (`services/api/tests/test_domain_sql_on_both_stores.py::test_a_sqlite_only_test_gets_the_sqlite_parameter_alone`).

### Honest limitations (amendment of 2026-10-12, B2)

- A test split in two keeps its domain half on both stores and its CLI
  half on SQLite:
  `services/api/tests/test_eligibility.py::test_the_academic_kind_is_not_assessed`
  and
  `services/api/tests/test_eligibility.py::test_the_cli_refuses_eligibility_on_the_academic_kind`.
- A SQLite-only test that mixes domain calls with the CLI or the API is
  proved on Postgres only through the tests of the same domain calls
  that run on both.
