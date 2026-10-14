---
spec: 139
title: Discovery places each posting by the ownership it demands
status: accepted
approved: yes
milestone: M9
depends: [011, 033, 092, 097, 105, 112, 137, 138]
---

# Spec 139: Discovery places each posting by the ownership it demands

## Problem

After spec 137 a focused track scores what is added to it by hand. Every
discovered posting still lands in the industry track. Bounded roles
therefore keep arriving in the search whose documents present a senior
engineer looking for ownership.

Two searches over the same sources would see the same postings, and a URL
can be in only one track (spec 092). Giving the focused track its own
watchlist would duplicate the industry one. The operator wants one
discovery run in which each posting gets a verdict from each search and
becomes one row, in the track whose ownership demand it matches. The
verdict from the other search stays visible on the row, so a misplaced
posting can be moved (spec 138) with the reason in view.

## Scope

- `services/api/src/harrier/screening/pipeline.py`: `screen_jobs` screens
  each posting with both kinds when a focused track exists, and places it.
- `services/api/src/harrier/screening/focused.py`: `ownership_demand`.
- `services/api/src/harrier/tracker/schema.py`: one migration, both
  dialects, adding `job_verdicts`. On Postgres it has the owner column and
  the row policy that spec 105 requires of every personal table. A guarded
  path; approved under this spec.
- `services/api/src/harrier/tracker/store.py`: the verdict write, inside
  the transaction that appends the row.
- `services/api/src/harrier/tracker/queue.py` and the CLI's `next` and
  `review`: the other verdict on each row's line.
- `services/api/src/harrier/tracks.py`: `discover` on a focused track
  still refused, with a message naming the shared run.
- Tests: `services/api/tests/test_shared_discovery.py` (new).

## Behavior

### Without a focused track

`harrier discover` behaves exactly as today: the same rows, scores,
`scoring_version`, seen records and decisions. `job_verdicts` stays empty.

### With one unarchived focused track

Every normalized posting that passes dedupe is screened twice, by the
industry path as today and by the focused path (spec 137). Each screen
gives a verdict: `pass` with a score, or `reject` with the gate's reason.

**The ownership demand.** `ownership_demand(job, document)` returns
`broad` when the title contains any of the focused document's
`broad_title_signals`, or the description contains any of its
`broad_description_signals`. Otherwise it returns `bounded`. Matching is
case-insensitive on whole words.

**Placement.**

| industry | focused | demand | row goes to |
| --- | --- | --- | --- |
| pass | pass | broad | industry |
| pass | pass | bounded | focused |
| pass | reject | any | industry |
| reject | pass | any | focused |
| reject | reject | any | no row; the seen record keeps both reasons |

The row is appended through the store as today, with its own track's score
and signals. Its `signals` adds `demand=<broad|bounded>`.

**The other verdict.** In the same transaction, one `job_verdicts` row is
written for the track the posting did not go to: `job_id`, `track_id`,
`verdict` (`pass` or `reject`), `score` (empty on reject), `reasons` (the
gate's reason, or the other track's signals), `scoring_version` and `at`.
There is at most one verdict per job and track. A later run does not
rewrite it, because a seen posting is not screened again (spec 011).

**Showing it.** `next` and `review` add to each row that has a verdict:
`also <slug>: <score>`, or `also <slug>: rejected (<reason>)`. A row moved
by spec 138 keeps its verdicts, and the line shows the verdict for the
track the row is not in, when there is one.

**Running it.** The shared run is the default `discover`, with no
`--track`, so the schedule (which never names a track) runs it unchanged.
`harrier --track <focused> discover` exits 2 with "the focused track is
discovered by the shared run: `harrier discover`".

## Failure modes

- **The focused track has no screening document.** `discover` runs the
  industry path alone, places every passing posting in the industry track
  as today, and prints one warning naming the track and the `profile put
  screening` command. A run never stops on this.
- **The focused track is archived.** Treated as no focused track.
- **A posting seen before the focused track existed.** Not screened again.
  `reconsider` (spec 097 opened it per track) is the operator's tool.
- **A failure after the row is appended.** The row and its verdict are one
  transaction. Neither is written without the other.
- **A demand signal that misreads a posting.** The row lands in the wrong
  track, the other verdict shows that it would have passed there, and
  `move` (spec 138) corrects it.

## Acceptance criteria

- [ ] With no focused track, a discovery fixture run produces byte-identical
  rows, events and seen records to the same run before this change (planned
  test_discovery_without_a_focused_track_is_unchanged).
- [ ] A remote EMEA "Junior Frontend Developer" posting with no broad
  signal lands in the focused track, and its `review` line shows
  `also <industry slug>: <score>` (planned test_a_bounded_posting_goes_to_the_focused_track).
- [ ] A remote EMEA "Staff Frontend Engineer" posting lands in the
  industry track, with a focused verdict on its line (planned
  test_a_broad_posting_stays_in_industry).
- [ ] A remote EMEA "Senior Frontend Engineer" posting with no broad
  description signal is `bounded` and lands in the focused track (planned
  test_a_senior_title_alone_is_not_broad).
- [ ] A posting whose description says "end-to-end ownership" under a
  junior title is `broad` and lands in the industry track (planned
  test_a_description_signal_makes_demand_broad).
- [ ] A posting rejected by both writes no row, and its seen record holds
  both reasons (planned test_a_posting_both_reject_has_no_row).
- [ ] With the focused track's screening document missing, every passing
  posting lands in industry and the run prints one warning (planned
  test_discovery_without_focused_configuration_falls_back_to_industry).
- [ ] On Postgres, a second owner cannot read the first owner's
  `job_verdicts` rows (planned test_job_verdicts_are_owned_and_policed).
- [ ] `just check` passes.

## Proof / origin

- The one screening path and its per-kind gates: `screen_jobs` in
  `services/api/src/harrier/screening/pipeline.py`; specs 011, 097.
- One URL in one track: spec 092.
- Every personal table owned and policed on Postgres: spec 105.
- Migrations in both dialects: spec 112.
- The operator's choice of 2026-10-14: one discovery run, one row per
  posting, the other track's verdict shown on the row.

## Out of scope

- The verdict in the API, the browser, the CSV export and the digest.
- Per-track sources or watchlists.
- Rescreening seen postings.
- A third track in the shared run.

## Migration

None for the operator beyond spec 137's setup. Rows that existed before
stay where they are. Spec 138 moves any that belong in the focused track.
