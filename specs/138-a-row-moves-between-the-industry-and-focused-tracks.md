---
spec: 138
title: A row moves between the industry and focused tracks
status: accepted
approved: yes
milestone: M9
depends: [079, 091, 092, 137]
---

# Spec 138: A row moves between the industry and focused tracks

## Problem

Every tracker row belongs to one track, and nothing moves it. The store
refuses a `track_id` in an update (`services/api/src/harrier/tracker/store.py`,
"track_id is set from the scope, not from the fields"). A URL can be in
only one track (spec 092), so the operator cannot re-add a posting to
another track either.

After spec 137 that matters. Rows already in the industry track include
bounded roles that belong in the focused track, some of them already
applied to. A later posting can also land in the wrong track when spec
139's ownership signals misread it. The operator needs to move the row and
keep its history: its status, its dates and its events.

## Scope

- `services/api/src/harrier/tracker/store.py`: `move_job`, the one write
  that changes `track_id`.
- `services/api/src/harrier/tracks.py`: `move` in `NON_DEFAULT_OPERATIONS`
  and `WRITE_OPERATIONS`.
- `services/api/src/harrier/tracker/reasons.py`: the reason code `moved`.
- `services/api/src/harrier_cli/main.py`: the `move` subcommand and its
  `COMMAND_CLASSES` entry (`database`).
- `services/api/src/harrier_api/settings_routes.py`: `move` is placed
  terminal-only in `COMMAND_PLACES` (amendment 4).
- Tests: `services/api/tests/test_move.py` (new).

## Behavior

`harrier [--track <from>] move <id> --to <slug>`.

- The row must be in the scope's track (spec 092). Otherwise exit 1 with
  "no job <id> on track <from>".
- The source and destination must be one industry track and one focused
  track, in either direction. Any other pair, including either side being
  academic, exits 2: an academic row carries a deadline and no score, and
  its queue and labels differ.
- In one transaction, `move_job`:
  1. updates the row's `track_id`, guarded by its current `track_id`,
  2. rescores the row with the destination kind's scorer from the cached
     description that the row's `created` event pins (spec 079), and
  3. appends one `decision` event with actor `candidate`, `from_status`
     and `to_status` both the row's current status, reason code `moved`,
     reason text "moved from <from> to <to>", and the new score and
     scoring version.
- Status, `applied_date`, `last_contact`, `next_action`, notes, the
  outreach fields and every earlier event are unchanged.
- It prints "moved <id> from <from> to <to>, score <old> to <new>".

## Failure modes

- **No cached description.** The row keeps its score. The event's reason
  text adds "not rescored: no cached description", and the printed line
  says the same.
- **A destination gate fails.** For example, the cached location is onsite
  and the destination is focused. The row still moves: the operator's
  decision outranks a gate. The event's reason text names the failed gate,
  and the printed line warns.
- **The same track.** Exit 2, nothing written.
- **A focused destination with no screening document.** Exit 2 naming the
  `profile put screening` command, nothing written, as a focused `add`
  refuses (amendment 1).
- **An archived source or destination.** Exit 2 naming the archived track
  (spec 093).
- **A concurrent move or delete.** The guarded update matches no row. The
  transaction rolls back, nothing is written, and the command exits 1 with
  "job <id> changed while moving; run it again".
- **The second run.** After a move the row is no longer in the source
  scope, so a repeat exits 1 as above. A move back is a new move with its
  own event.

## Acceptance criteria

- [ ] An industry row with status `applied` moves to the focused track.
  `--track <focused> next` lists it with status `applied` and the same
  `applied_date`, and `events show` ends with the `moved` event (`services/api/tests/test_move.py::test_a_move_keeps_status_dates_and_history`).
- [ ] The moved row's score equals the focused scorer's score on its cached
  description, and the event carries that score and version (`services/api/tests/test_move.py::test_a_move_rescores_from_the_cached_description`).
- [ ] A row with no cached description keeps its score, and the event says
  it was not rescored (`services/api/tests/test_move.py::test_a_move_without_a_description_keeps_the_score`).
- [ ] A move to or from an academic track exits 2 and writes nothing
  (`services/api/tests/test_move.py::test_a_move_involving_an_academic_track_is_refused`).
- [ ] A move of an id that is not in the scope's track exits 1 (`services/api/tests/test_move.py::test_a_move_reads_only_the_scopes_row`).
- [ ] The industry `review` no longer lists the moved row, and every other
  industry row prints as before (`services/api/tests/test_move.py::test_a_move_leaves_other_rows_alone`).
- [ ] A move to a focused track with no screening document exits 2 and
  writes nothing (amendment 1; `services/api/tests/test_move.py::test_a_move_to_an_unconfigured_focused_track_is_refused`).
- [ ] A move that finds the row moved underneath it exits 1 and writes
  nothing (`services/api/tests/test_move.py::test_a_row_moved_underneath_is_a_conflict`).
- [ ] `just check` passes.

## Amendments found while implementing (2026-10-14)

1. **A focused destination needs its screening document.** The spec named
   no outcome for a move into a focused track that has none. The move is
   refused with exit 2 and the same message a focused `add` gives (spec
   137), because the destination kind's scorer has nothing to score with,
   and a move that silently kept an industry score would rank the row by
   the other track's weights.
2. **`next`, not `review`, shows a moved applied row.** `review` lists only
   rows still awaiting a decision, so an applied row appears there only in
   the counts. The first criterion names `next`.
3. **`moved` is not a rejection code.** `REASON_CODES` in
   `services/api/src/harrier/tracker/reasons.py` is the rejection and
   outcome table: the command line offers it to `reject`, and the API
   publishes its candidate and system codes as `RejectionCode`. `moved` is
   a separate constant there, so neither changes, and the contract does
   not. The event is still the candidate's `decision`; its status does not
   change, so the learned score's labels
   (`services/api/src/harrier/scoring/labels.py`) never take it as the
   deciding event.

4. **`move` has a place on the settings page.** Every CLI subcommand sits
   in exactly one place in `COMMAND_PLACES`
   (`services/api/src/harrier_api/settings_routes.py`), and
   `services/api/tests/test_ui_settings.py::test_every_cli_subcommand_has_exactly_one_place`
   fails for a command in none. `move` is terminal-only, as Out of scope
   keeps a move control out of the browser. The page lists it with that
   reason; no route is added and the contract does not change.

## Proof / origin

- The refusal of a `track_id` in an update:
  `services/api/src/harrier/tracker/store.py`; spec 091.
- The triggers that keep a row's track existing:
  `services/api/src/harrier/tracker/schema.py`
  (`jobs_track_must_exist_on_update`); spec 091.
- One URL in one track: spec 092.
- The append-only event log and the description hash the `created` event
  pins: `job_events` in the same schema file; spec 079.

## Out of scope

- Bulk moves, and moves by filter.
- A move route in the API, and a move control in the browser.
- Moving outreach contacts or briefs. They are keyed by job id and follow
  the row unchanged.
- Rescoring with anything but the cached description: no network fetch.

## Migration

None. No schema change: the triggers from spec 091 already check that the
destination track exists.
