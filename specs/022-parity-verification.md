---
spec: 022
title: Parity verification: checklist and shadow-run diff
status: shipped
approved: yes
milestone: M5
depends: [020,021]
---

# Spec 022: Parity verification: checklist and shadow-run diff

## Problem

Both systems run side by side until verified parity. "Verified" needs to
mean something checkable, or cutover happens on a feeling.

The stub covered verification and the cutover event together. They split
here: everything below is read-only with respect to the old system and can
run today, repeatedly, at no cost. The switchover and archive are a single
irreversible sitting that repoints a live job search, and are spec 024.

## Scope

- harrier.parity.matrix: docs/parity-matrix.md parsed as data. The matrix
  is the inventory the cutover plan says the checklist is generated from,
  so it is parsed rather than retyped. A malformed row raises instead of
  being skipped: a checklist quietly missing an item is worse than one
  that fails to generate.
- harrier.parity.checklist: one item per matrix row, worded by verdict
  (keep asks whether behavior is identical, change asks whether the
  difference is the intended one, drop asks for confirmation). Ticks and
  waivers round-trip through regeneration, and decisions recorded against
  rows the matrix no longer has are reported as retired rather than
  dropped.
- harrier.parity.diff: two run summaries compared per source. Both systems
  write the same shape, because harrier ported it.
- discovery gains --shadow: dry-run semantics plus no paid source.
- CLI: harrier parity checklist [--out], harrier parity status
  [--checklist], harrier parity diff --old --new. Non-zero exit when the
  checklist is incomplete or the diff is not clean, so the dual-run period
  can be driven from a scheduled job.

## Inputs, outputs, failure modes

- Inputs: docs/parity-matrix.md, an existing checklist when regenerating,
  and two run summaries (an aggregate `job_imports_run.json` or a
  per-source `<source>_latest.json`).
- Outputs: docs/parity-checklist.md, a status line, and a diff report.
- Failure modes: a malformed matrix row, verdict, or missing file raises
  MatrixError naming the line; an unreadable summary raises
  RunSummaryError naming the path; a matrix whose stated totals disagree
  with its own table exits non-zero.

## What the diff can and cannot prove

Run summaries are compared, not screening decisions replayed. Three limits,
all of them stated in the report rather than left for the reader to infer:

1. The two systems fetch at different moments, so a posting in one run and
   not the other is usually a board change. Reported separately from
   divergences that are decidable.
2. Screening counts are only comparable when both runs suppressed a similar
   share of postings as already seen, and when both fetched a similar number
   of postings. Neither holds before the seen-state migration, which the
   cutover plan now performs at the start of the dual-run period (phase 2a)
   rather than at cutover. The diff refuses to compare rather than presenting
   the artifact as findings.
3. The stronger proof, replaying one recorded input through both screeners,
   would mean importing the old repo's modules. The old repo is read-only
   by rule (cutover plan phase 0), so it is out of scope.

The fetch counts are compared regardless, because they are the one thing
provable before migration and they are what says the importers agree.

## Stated changes from the old code

There is no old code here; the old system is the thing being compared
against. Two changes to harrier itself:

- discovery gains --shadow. --dry-run alone still starts a billed Apify run
  and discards the result, inherited from the old orchestrator, where it is
  a tolerable cost for a one-off preview. The dual-run period runs discovery
  on a schedule for a week, so it needs a mode that is free to repeat.
  --dry-run keeps its meaning; the cost is now stated in its help text
  rather than being a silent surprise.
- docs/parity-matrix.md's stated totals are corrected from 58/20/15 to
  60/20/16 across 96 rows. The document undercounted its own table by three
  rows, which is exactly the gap the checklist exists to close, and the
  generator is what found it.

## Findings from running this against production

Recorded because they are the point of the exercise, and because the second
one changed the tool:

- The free importers agree on live data. A shadow run fetched 7,412
  Greenhouse, 9,810 Ashby, 1,071 Lever, and 100 RemoteOK postings against
  the old system's 7,416 / 9,811 / 1,071 / 100 in its run the same morning.
- Screening counts were not comparable at all, and the first version of the
  diff reported twelve divergences that were entirely an artifact: the old
  system had already seen almost every posting, so it screened 11 of 7,416
  Greenhouse results, while a fresh harrier store screened 6,531. The diff
  now detects this and declines to compare, and docs/cutover-plan.md moves
  the seen-state migration into a new phase 2a. As written, phase 2's exit
  criteria were unreachable: they required a clean diff that could not be
  produced until a phase 3 step had run.

## Acceptance criteria

- [x] every matrix row becomes a checklist item worded by its verdict
      (test_every_row_becomes_an_item_with_its_verdict_prompt)
- [x] regenerating preserves ticks and waivers, and reports items the
      matrix no longer carries
      (test_regenerating_preserves_ticks_and_waivers,
      test_a_retired_item_is_reported_not_silently_dropped)
- [x] a malformed row or verdict fails loudly rather than dropping an item
      (test_an_unreadable_verdict_fails_rather_than_dropping_the_row,
      test_a_short_row_fails_rather_than_being_guessed)
- [x] the matrix's stated totals and its table cannot drift
      (test_stated_counts_match_the_table)
- [x] identical runs are clean; a rescored posting is a divergence; a
      posting only one run saw is not
      (test_identical_runs_are_clean,
      test_a_score_change_on_the_same_posting_is_decidable,
      test_a_posting_only_one_run_saw_is_not_counted_as_a_divergence)
- [x] a seen-state asymmetry blocks the screening comparison instead of
      producing findings, while fetch counts are still reported, and the
      comparison is by suppressed share rather than absolute count
      (test_seen_state_asymmetry_blocks_the_screening_comparison,
      test_matching_fetch_counts_are_reported_even_when_screening_is_blocked,
      test_equal_suppressed_shares_do_not_block_on_unequal_fetch_counts)
- [x] differing fetch counts also block the screening comparison, and a
      source present in only one run is not clean
      (test_differing_fetch_counts_block_the_screening_comparison,
      test_a_source_only_the_new_run_had_is_not_clean,
      test_a_source_missing_from_the_new_run_is_not_clean)
- [x] an object that is not a run summary is rejected rather than diffing
      clean (test_an_object_that_is_not_a_run_summary_is_rejected)
- [x] an unrecognized table header fails rather than skipping its rows
      (test_an_unrecognized_table_header_fails_rather_than_skipping_its_rows)
- [x] a retired decision keeps its tick across regenerations and keeps the
      checklist incomplete until a human resolves it
      (test_a_retired_decision_keeps_its_tick_across_regenerations,
      test_a_retired_decision_keeps_the_checklist_incomplete)
- [x] the committed checklist cannot drift from the matrix, and the
      header's own example is not read as a decision
      (test_the_committed_checklist_matches_the_matrix,
      test_the_headers_own_example_is_not_parsed_as_a_decision)
- [x] no report carries a filesystem path
      (test_report_carries_no_filesystem_paths)
- [x] a shadow run is a dry run and never reaches the paid source
      (test_shadow_implies_dry_run,
      test_a_shadow_run_never_reaches_the_paid_source,
      test_a_shadow_run_writes_nothing_to_the_tracker)
- [x] All gates green on PR (PR #19)

## Proof / origin

docs/cutover-plan.md phases 1 and 2; docs/parity-matrix.md. Proving file:
services/api/tests/test_parity.py.

Honest limitations: the diff reads summaries, so it inherits everything in
"What the diff can and cannot prove" above. The checklist verifies that a
human made a decision about every row; it cannot verify that the decision
was correct. Exit codes make the dual-run period automatable, but nothing
here decides that parity is reached: that judgement is Akin's, in spec 024.

## Out of scope

The cutover event: quiesce, final migration, switchover, fallback window,
archive (spec 024). Performing the phase 2a seen-state migration, which uses
spec 004's migration machinery and is an operational step rather than new
code. Replaying recorded inputs through the old screener.
