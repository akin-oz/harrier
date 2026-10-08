---
spec: 092
title: Every reader of tracker rows reads one track
status: accepted
approved: yes
milestone: M9
depends: [076, 077, 091]
---

# Spec 092: Every reader of tracker rows reads one track

## Problem

Spec 091 gives every row a track and changes no reader. So after it, the
queue, the digest, the export, the selector, the API job list, the learned
model's label export, the batch evaluation, the outreach sync, the mail
watch and `reevaluate` all still read every row in the file. That is
correct only while every row is in track 1, which is exactly the condition
spec 093 ends.

The readers are not a short list. At cd5665d, `list_jobs` is called from
`harrier/digest.py:283`, `harrier/discovery.py:176`,
`harrier/offers/batch.py:66`, `harrier/mail/watch.py:578`,
`harrier/tracker/selector.py:49`, `harrier/scoring/export.py:60`,
`harrier/tracker/export.py:43`, `harrier/tracker/actions.py` (four
sites), `harrier/outreach/backfill.py:49`, `harrier/outreach/joblink.py`
(two sites), `harrier/outreach/state.py:152`,
`harrier/screening/reconsider.py:76`, `harrier_api/app.py:186` and
`harrier_cli/main.py` (three sites); `get_job` from a further fourteen.
Each one would have to remember a predicate, forever, and the one that
forgets fails silently: it returns rows from the wrong search and nothing
notices until a person does.

The fix has to be a shape the type checker and a test can hold, not a
convention. A reader that cannot be called without saying which track it
reads cannot forget.

## Scope

- `services/api/src/harrier/tracks.py`: nothing new; `Scope` is spec 091's.
- `services/api/src/harrier/tracker/store.py`: `get_job`, `list_jobs`,
  `list_events`, `set_status`, `update_fields`, `backfill_events` and the
  helpers under them take a `Scope` as a required positional parameter
  after `conn`. `find_duplicate` and a new `all_tracks_dedupe_rows` are the
  named cross-track reads.
- `services/api/src/harrier/tracker/selector.py`, `queue.py` (no change:
  it ranks rows it is given), `actions.py`, `export.py`,
  `invariants.py` if it reads rows.
- `services/api/src/harrier/digest.py`, `discovery.py`, `capture.py`,
  `artifacts.py`, `resume/tailor.py`, `offers/batch.py`, `mail/watch.py`,
  `outreach/backfill.py`, `outreach/joblink.py`, `outreach/state.py`,
  `screening/reconsider.py`, `scoring/export.py`: each takes or resolves a
  scope as described under Behavior.
- `services/api/src/harrier_api/deps.py`: a `get_scope` dependency beside
  `get_conn`. `services/api/src/harrier_api/app.py`,
  `outreach_routes.py`, `capture_routes.py`, `mail_routes.py`: routes that
  read rows take it.
- `services/api/src/harrier_cli/main.py`: the default scope is resolved
  once after the connection is opened and passed to every command that
  reads rows.
- `services/api/tests/test_track_isolation.py` (new) and
  `services/api/tests/test_tracker_queries_name_their_track.py` (new), plus
  signature updates in existing tests.

Not touched: the schema, the contract (`GET /jobs` and every other route
keep their shapes; there is no track parameter until the web spec), the web
app, the schedule, the plists, the classification table.

## Behavior

### A scope is required, never defaulted

Every store function that reads or writes `jobs` or reads `job_events`
takes a `Scope` with no default. A call without one is a `TypeError` at
the call site and a pyright error before that. Readers filter with
`WHERE track_id = ?` from the scope; a by-id read (`get_job`, `set_status`,
`update_fields`, `list_events`) adds the same predicate, so a job id from
another track is `JobNotFoundError`, the same error a missing id raises.
The message names the id and nothing about the other track's row.

`rank_active` and `status_counts` are unchanged: they rank and count the
rows they are handed, which are already one track's.

### Each entry point resolves the default scope once

The scope is resolved where the connection is opened and passed down.
Nothing inside the domain resolves it again, and nothing reads a global.

| Entry point | Where the scope is resolved |
|---|---|
| CLI (`harrier_cli/main.py`) | once in the command dispatcher, after `connect()`, with `default_scope(conn)`; spec 093 replaces the argument with the `--track` flag's value |
| API | a `get_scope` dependency that depends on `get_conn`, resolved per request, `default_scope` until a later spec adds track selection to the API |
| discovery (`run_discovery`) | takes a `Scope` from the CLI; every row it adds lands there |
| digest (`build_digest`), mail watch, batch evaluation, outreach sync, label export, reconsider | take a `Scope` from the CLI |
| run manager subprocesses | are the CLI, so they resolve as the CLI does |

### Dedupe stays global for url and external_key

The unique indexes on `jobs.url` and `jobs.external_key`
(`services/api/src/harrier/tracker/schema.py:133-137`) are global, so a url
stored in one track is a duplicate in every other: one posting lives in
exactly one track. Two reads are therefore cross-track by name:

- `find_duplicate` reads url and external_key across all tracks. Its
  `DuplicateJobError` names the existing row's id and track slug, so the
  operator learns where the posting already lives.
- `all_tracks_dedupe_rows(conn)` returns the url, external_key, company,
  title and track_id of every row and nothing else, and is called only by
  `build_tracker_indexes` in discovery. `list_jobs` is no longer used for
  that index.

Whether company plus title dedupes per track or across tracks is an open
decision below. The proposal is across tracks, so a posting entered on two
tracks is refused with the existing track named rather than tracked twice.

### Labels, training and the mail watch

- `scoring export` takes the scope. The label export, and so the learned
  model's training set (spec 077), reads one track's rows and events. The
  industry model trains on the industry track only; an academic track's
  decisions never reach it. The export report records the track slug.
- `scoring train` reads only the export and is unchanged.
- The mail watch matches messages against the resolved scope's rows only.
  Its event record in `data/gmail-watch/events.jsonl` gains a `track` field
  holding the matched job's slug, so a later reader of that file can tell
  which search a message belonged to. The watch does not write
  `job_events` today and does not start to.
- `reconsider`'s human-rejected lookup
  (`services/api/src/harrier/screening/reconsider.py:68`) reads one track.
  A posting the operator rejected on another track is not protected on
  this one; that is correct, because the other track's rejection was made
  under other rules.
- `evaluate-prospects` (`offers/batch.py`) evaluates one track's prospects
  and auto-rejects within it.

### `events backfill` is per scope

`harrier events backfill` reconstructs history from each row with no
events. The reconstruction rules are the industry track's (an `applied`
row seeds a follow-up, for example), so the backfill takes the scope and
touches one track's rows.

### The static guard

Behavioral tests prove today's readers. A reader written next month has no
behavioral test yet, and the failure it would introduce is silent. So one
test reads the source, and it is written as the exception
`.ai/rules/review-response.md` (lines 19 to 21) allows: a source-reading
test is a last resort because it breaks on a wrapped line and can pass for
the wrong reason. Both are addressed, and the test is held to a fixture
that proves it fails.

The guard (`services/api/tests/test_tracker_queries_name_their_track.py::test_every_tracker_query_names_its_track`) reads every
Python file under
`services/api/src`, finds every string literal that names `jobs` or
`job_events` as a table (`FROM jobs`, `JOIN jobs`, `UPDATE jobs`, `INSERT
INTO jobs`, `DELETE FROM jobs`, `FROM job_events`, `JOIN job_events`), with
whitespace normalised so a wrapped literal reads as one line, and asserts
each one either:

- contains `track_id` in a `WHERE` or `JOIN` clause, or names `track_id`
  among its inserted columns; or
- is an `INSERT INTO job_events`, exempt because it names a `job_id` that
  was resolved in scope; or
- is one of the named exemptions below.

Any literal the test cannot classify fails the test. A literal it never
finds because the SQL is assembled from pieces is the stated blind spot;
the behavioral tests cover the readers that exist, and review covers the
rest.

Named exemptions, each a (file, function) pair the test asserts still
exists, so an exemption cannot outlive its function:

| Where | Why it reads across tracks |
|---|---|
| `tracker/store.py`, `find_duplicate` | url and external_key are globally unique |
| `tracker/store.py`, `all_tracks_dedupe_rows` | the dedupe index feed |
| `tracker/migrate_legacy.py`, the import | a whole-database import that stamps track 1 explicitly, and a `--replace` that empties the file |
| `backup.py`, `cutover.py`, `harrier_api/app.py` health | `SELECT COUNT(*)` over the file, a check on the database and not a read of a search |

`profile_documents` and `user_config` are not covered by the guard. They
join it when per-track documents land and carry a track column.

### The industry track is unchanged

Every row is in track 1 and every entry point resolves track 1, so every
command, route and scheduled run returns what it returned before. The
proof is the existing suite passing with nothing changed but signatures.

## Failure modes

- **A reader called without a scope.** `TypeError`, and pyright before it.
- **A job id from another track.** `JobNotFoundError`, naming the id only.
  The browser, which sends ids, cannot move a row of another track through
  the default scope.
- **A url or external_key already stored in another track.**
  `DuplicateJobError` naming the existing id and its track slug. The row is
  not added. Discovery counts it as `skipped_tracker_duplicate`, as it
  counts a same-track duplicate today.
- **A scope whose track is archived.** Reads succeed. Writes are spec
  093's to refuse; nothing in this spec can name a non-default track from
  outside a test.
- **A query the static guard cannot classify.** The test fails and names
  the file and the literal. The author adds the predicate or a justified
  exemption.
- **A mail event file written before this spec.** Its records have no
  `track` field; a reader treats a missing field as the default track.

Must not introduce: a reader with a `scope=None` default; a module-level
or process-global current track; a second resolution of the default scope
inside the domain; a change to any route's shape in the contract; a change
to what the industry track returns; a write to `job_events` from the mail
watch; a test that opens the operator's data directory (spec 060).

## Acceptance criteria

Tests in `services/api/tests/test_track_isolation.py` unless named
otherwise. Each builds a database under `tmp_path`, inserts a second track
directly into `tracks` (no command creates one until spec 093), and adds
synthetic rows to both.

- [x] `next`, `review`, the API queue and the API job list over one scope
      never return a row of the other track
      (`services/api/tests/test_track_isolation.py::test_queue_never_shows_another_tracks_rows`)
- [x] The label export over one scope holds no row or event of the other
      track, so the model trains on one track
      (`services/api/tests/test_track_isolation.py::test_training_labels_ignore_other_tracks`)
- [x] The digest and the CSV export over one scope name no row of the
      other (`services/api/tests/test_track_isolation.py::test_digest_and_export_read_one_track`)
- [x] `add_job` in the second track with a url, and separately an
      external_key, already stored in the first raises `DuplicateJobError`
      naming the first track's slug, and the row is not added
      (`services/api/tests/test_track_isolation.py::test_a_url_stored_in_one_track_is_a_duplicate_in_another`)
- [x] `get_job`, `set_status`, `update_fields` and `list_events` with a job
      id from the other track raise `JobNotFoundError` naming the id only,
      and the row is unchanged
      (`services/api/tests/test_track_isolation.py::test_a_row_outside_the_scope_is_not_found_by_id`)
- [x] The mail watch matches only the scope's rows and its event record
      carries the matched job's track slug
      (`services/api/tests/test_track_isolation.py::test_mail_matching_reads_one_track_and_records_it`)
- [x] `events backfill` over one scope writes events for that track's
      rows only (`services/api/tests/test_track_isolation.py::test_backfill_reconstructs_one_track`)
- [x] `reconsider` protects only the scope's human rejections, and
      `evaluate-prospects` evaluates only the scope's prospects
      (`services/api/tests/test_track_isolation.py::test_reconsider_and_batch_evaluation_read_one_track`)
- [x] Every SQL literal on `jobs` or `job_events` under `services/api/src`
      names its track or is a named exemption whose function exists
      (`services/api/tests/test_tracker_queries_name_their_track.py::test_every_tracker_query_names_its_track`), and the guard
      fails against a fixture literal that lacks the predicate and against
      an exemption naming a function that does not exist
      (`services/api/tests/test_tracker_queries_name_their_track.py::test_the_static_guard_fails_on_an_unscoped_query`)
- [x] The CLI resolves the default scope once per invocation and the API
      once per request, proven by counting `tracks` reads with a trace
      callback (`services/api/tests/test_track_isolation.py::test_the_default_scope_is_resolved_once_per_entry`)
- [x] The industry track is unchanged: the full suite passes with
      signature updates only, and
      `services/api/tests/test_tracker_cli.py::test_rank_puts_the_nearest_to_sending_first`
      and
      `services/api/tests/test_tracker_cli.py::test_review_lists_only_rows_awaiting_a_decision`
      pass without modification to their assertions
- [x] The contract is regenerated by `just contract` and shows no diff
- [x] Each test above fails with its behavior removed, checked by removing
      each behavior in turn and recorded in the pull request
- [x] No real tracker row, count or posting appears in a fixture, this
      spec or a commit message (ADR-008)
- [ ] All gates green on the pull request

## Honest limitations

- **The static guard reads string literals.** SQL built from pieces at
  runtime is invisible to it. The store builds a few queries with
  f-strings for column lists; their table name and predicate are literal,
  which is what the guard reads, and the test pins that shape.
- **Isolation is by predicate, not by store.** A track is the same
  person's data, so a missed predicate shows the wrong search and not the
  wrong person (ADR-012). That is why a source-reading guard is acceptable
  here and would not be for tenants.
- **The second track in the tests is inserted by hand.** Until spec 093 no
  command creates one, so the behavioral tests prove the readers against
  a row the test wrote.
- **No command can name a track yet.** This spec makes every reader able
  to read one; spec 093 makes the CLI able to say which. Between the two,
  the default scope is the only scope outside a test.
- **The mail watch's older event records carry no track.** Read as the
  default track, which is what they were.

## Migration

None for the operator. No schema change, no data change. `just
container-up` so the image carries the scoped readers.

## Options weighed

- **A default scope on every reader** (`scope: Scope | None = None`).
  Rejected: it is spec 041's "parameter that exists to be defaulted", and a
  forgotten caller would read every track with no error.
- **A context variable holding the current track.** Rejected: invisible
  in signatures, wrong under the API's threadpool, and untestable for
  "two scopes do not mix".
- **A scoped repository object** wrapping the connection. Rejected for
  now: it is a larger refactor of the store for the same guarantee a
  required parameter gives; ADR-013's hosted store may want it, and this
  spec does not foreclose it.
- **Behavioral tests only, no static guard.** Rejected: they prove the
  readers that exist. The guard is the one test that reads a reader
  written after this spec.

## Open decisions for Akin

1. **Company plus title dedupe: per track or across tracks.**
   Recommendation: across tracks, with the existing track named in the
   refusal. One posting lives in one track, and the operator who
   entered it twice is better served by being told where it is than by a
   second row.
2. **Whole-database counts stay unscoped** (`backup`, `cutover`, the
   health route). Recommendation: yes; they describe the file, and they are
   named exemptions so the choice is visible.
3. **`events backfill` per scope** rather than over the whole file.
   Recommendation: per scope, because the reconstruction rules are a
   kind's rules and spec 093 gives the academic kind different ones.

## Proof / origin

- The readers: every `list_jobs` and `get_job` call site named under
  Problem, at cd5665d.
- The store: `services/api/src/harrier/tracker/store.py` (`get_job` at
  line 156, `list_jobs` at 163, `set_status` at 219, `update_fields` at
  332, `list_events` at 445, `backfill_events` at 522).
- The dedupe reads and their indexes: `store.py:77-96`,
  `schema.py:133-137`, `discovery.py:176`,
  `screening/pipeline.py:64` (`build_tracker_indexes`).
- The mail watch's event file: `services/api/src/harrier/mail/watch.py`
  (`append_event`, `tracker_rows` at line 578).
- Labels from events: spec 077 ("Label"), spec 079;
  `services/api/src/harrier/scoring/export.py:60-62`.
- The API dependency to extend: `services/api/src/harrier_api/deps.py`
  (`get_conn`).
- The exception that allows a source-reading test:
  `.ai/rules/review-response.md:19-21`.
- Connections closed by their opener: spec 076. The scope value and the
  default track: spec 091, ADR-012.

## Out of scope

- Naming a track from the CLI, the API or the browser (spec 093 for the
  CLI; later specs for the rest).
- Per-track profile documents and configuration, and the guard's
  extension to those tables.
- A track parameter on any route, or any contract change.
- Changing the queue's ranking (spec 093 adds the academic kind's).
- A scoped repository object (Options weighed).

## Amendment (2026-10-08, during implementation)

What implementation found, each with the test or file that holds it:

- **A by-id read of another track's events is a not-found, not an empty
  list.** `list_events` checks the job through `get_job` first, so the four
  by-id readers refuse the same way with the same message
  (`services/api/tests/test_track_isolation.py::test_a_row_outside_the_scope_is_not_found_by_id`).
- **Two more `job_events` readers join `jobs`.** `company_has_responded`
  and the private `_has_events` took a job id resolved in scope and read
  `job_events` by it alone; both now join `jobs` and filter on `track_id`,
  so the static guard reads them without an exemption.
- **Each SQL literal names its own track.** `list_jobs` and `add_job`
  assembled the predicate and the column list outside the literal, which a
  literal-reading guard cannot see; the literal now carries `WHERE
  track_id = ?` and `, track_id` itself. The guard reads a whole f-string
  once rather than its constant halves, proven by the `halves` function in
  its fixture
  (`services/api/tests/test_tracker_queries_name_their_track.py::test_the_static_guard_fails_on_an_unscoped_query`).
- **The named exemptions, as landed:** `find_duplicate` and
  `all_tracks_dedupe_rows` in the store; `migrate` in the legacy import;
  the whole-file counts in `verify_database`, `tracker_check`, `verify` and
  the API `health` route. The schema module is skipped as DDL. The
  `all_tracks_dedupe_rows` feed returns url, external_key, company, title,
  notes and track_id: `build_tracker_indexes` reads an external key out of
  the notes when the column is empty, so notes travel with it.
- **The mail watch's archived event gains the `track` key.** The archive
  keeps a fixed field list (spec 049's boundary, pinned by
  `services/api/tests/test_ui_inbox.py::test_the_archive_still_holds_only_what_it_held`);
  `track` joins it as a slug, the name of a search and nothing about a
  person. No match, no track: the key holds an empty string.
- **The CLI resolves the scope in twenty-one handlers, once each,** right
  after the connection they open; handlers that read no tracker rows
  resolve none. The API resolves it in `get_scope`, a dependency beside
  `get_conn`, once per request. The contract is unchanged: a dependency
  with no request parameter adds nothing to the OpenAPI document, and
  `just contract` shows no diff.
- **Company plus title dedupe is across tracks** (Open decisions, item 1,
  as recommended): `find_duplicate` is unchanged and the refusal names the
  existing row's track.
- **Mutants.** With the track predicate removed from `list_jobs`, from
  `get_job`, from `list_events`, from `backfill_events`, and with the mail
  event's track blanked, a test in `test_track_isolation.py` fails in each
  case; recorded in the pull request.
