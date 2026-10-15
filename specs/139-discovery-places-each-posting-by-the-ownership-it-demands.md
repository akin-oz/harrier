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
- `services/api/src/harrier/discovery.py`: the run resolves the focused
  track once, warns once when it has no screening document, and appends
  each row to its own track with its verdict (amendment 2).
- `services/api/src/harrier/screening/reconsider.py`,
  `services/api/src/harrier/screening/policy.py` and the CLI's
  `reconsider`: `reconsider` on either track (amendment 1).
- Tests: `services/api/tests/test_shared_discovery.py` (new), with
  `job_verdicts` added to the per-table lists of
  `services/api/tests/test_owner_policy.py`,
  `services/api/tests/test_dialect_parity.py`,
  `services/api/tests/test_seam_on_both_stores.py` and
  `services/api/tests/test_domain_sql_on_both_stores.py`, as
  `job_eligibility` is in each.

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

- [ ] With no focused track, a discovery run writes no `demand` signal and
  no verdict, and its seen state records the industry policy version, as
  before this change (amendment 4; `services/api/tests/test_shared_discovery.py::test_discovery_without_a_focused_track_is_unchanged`).
- [ ] A remote EMEA "Junior Frontend Developer" posting with no broad
  signal lands in the focused track, and its `review` line shows
  `also <industry slug>: <score>` (`services/api/tests/test_shared_discovery.py::test_a_bounded_posting_goes_to_the_focused_track`).
- [ ] A remote EMEA "Staff Frontend Engineer" posting lands in the
  industry track, with a focused verdict on its line (`services/api/tests/test_shared_discovery.py::test_a_broad_posting_stays_in_industry`).
- [ ] A remote EMEA "Senior Frontend Engineer" posting with no broad
  description signal is `bounded` and lands in the focused track (`services/api/tests/test_shared_discovery.py::test_a_senior_title_alone_is_not_broad`).
- [ ] A posting whose description says "end-to-end ownership" under a
  junior title is `broad` and lands in the industry track (`services/api/tests/test_shared_discovery.py::test_a_description_signal_makes_demand_broad`).
- [ ] A posting rejected by both writes no row, and its seen record holds
  the reason both screens share (amendment 3; `services/api/tests/test_shared_discovery.py::test_a_posting_both_reject_has_no_row`).
- [ ] A posting only the focused title rule passes lands in the focused
  track, and its line shows `also <industry slug>: rejected (title)`
  (`services/api/tests/test_shared_discovery.py::test_a_posting_only_the_focused_track_passes`).
- [ ] With the focused track's screening document missing, every passing
  posting lands in industry and the run prints one warning (`services/api/tests/test_shared_discovery.py::test_discovery_without_focused_configuration_falls_back_to_industry`).
- [ ] On Postgres, a second owner cannot read the first owner's
  `job_verdicts` rows
  (`services/api/tests/test_owner_policy.py::test_owner_a_reads_nothing_of_owner_b`,
  which walks every owned table, `job_verdicts` among them).
- [ ] `reconsider` on a focused track reads the shared seen state, reopens
  a rejection a changed focused document made stale, and never reopens a
  posting rejected by hand on either track (amendment 1; `services/api/tests/test_shared_discovery.py::test_reconsider_on_a_focused_track_reads_the_shared_state`,
  `services/api/tests/test_shared_discovery.py::test_reconsider_never_reopens_a_focused_rejection`).
- [ ] `just check` passes.

## Amendments found while implementing (2026-10-14)

1. **`reconsider` runs on either track, over the shared state.** Spec 137's
   amendment 6 held `reconsider` on the focused kind until this spec, and
   this spec named it as the operator's tool without saying what it does
   there. Owner's choice (option 1), 2026-10-14: one discovery run judges a
   posting for both tracks, so both tracks reconsider the same seen state,
   the industry track's. The current version is the one a shared run
   records (amendment 2). A posting rejected by hand on the industry track
   or on any focused track protects its entry
   (`reconsider_source(also=...)` in
   `services/api/src/harrier/screening/reconsider.py`). With no focused
   track, `reconsider` is what it was.
2. **A shared run records a shared version.** The seen state's version is
   what makes a decision stale (spec 031). With a configured focused track,
   a shared run records a digest of the industry and the focused versions
   (`shared_policy_version` in `services/api/src/harrier/screening/policy.py`),
   so a change to the screening document reopens its rejections. Without
   one, the industry version is recorded unchanged.
3. **Both screens reject at one gate.** Every gate but the title rule is
   the same for both kinds (spec 137): the hold list, remote and EMEA
   scope, dedupe and the LinkedIn page check. So a posting both reject is
   rejected at the title rule by both, or at one shared gate before either
   scores, and the seen record's one reason is both screens' reason.
   `screen_jobs` runs those gates once.
4. **"Byte-identical" is stated as what a test can check.** The first
   criterion said a run without a focused track is byte-identical to one
   before the change. No test can run the old code; the criterion now names
   what differs when a focused track exists (a `demand` signal, a verdict,
   a shared version) and checks that none of it appears without one.
5. **The CLI prints the verdict, not the queue.** `next` and `review` print
   the `also` line under each row; `services/api/src/harrier/tracker/queue.py`
   orders rows and is unchanged.

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
