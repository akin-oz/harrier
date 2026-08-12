---
spec: 033
title: The score means one thing, and says which policy produced it
status: accepted
approved: yes
milestone: M6
depends: [007, 027]
---

# Spec 033: The score means one thing, and says which policy produced it

## Problem

Four defects that all reduce to the same thing: the number the whole product
ranks by is not trustworthy.

**The cutoff cannot fire.** `SCORE_CUTOFF` is 55 against an arithmetic floor
of 59 for anything that reaches it: base 30, plus a remote bonus of 10 and a
preferred-region bonus of 8 that are both unconditional by the time scoring
runs (the gates already required the same match), plus a minimum include
keyword and skill signal. The existing test has to zero five configuration
values and empty both weight dictionaries to manufacture a single low-score
rejection. A threshold nothing can fail is decoration, and it hides the fact
that the real filter is entirely in the gates.

**`reevaluate` destroys the number it recomputes.** The CLI builds the job
with `description=""`, and scoring reads the description in three places. The
verb whose stated purpose is rescoring against current configuration scores
against strictly less input than the first pass had, then overwrites the real
value and the signals.

**The tracker has two score columns and they diverge.** `reevaluate` writes
`score` only, while the digest, notifications, resume tailoring, letters,
answers, and the API's `fit_score` all read `fit_score`. After one
`reevaluate`, the CLI queue and the nightly digest rank the same tracker by
two different numbers, and the notes string holds a third. The test cited as
proof asserts only that the fields are non-empty and would pass against a
constant.

**Scores are not comparable across time.** The scoring configuration is read
live at call time and a bare integer is stored with no version, yet ranking
sorts across months of stored values. A weight change silently re-ranks
history without recomputing it.

Found by the `principal-review` board (spec 028); the divergence was raised
independently by three of its five lenses.

## Scope

**One score.** One column is the score and the other is derived or removed.
Whichever survives, every reader uses it, and a test asserts that no writer
updates one without the other.

**A cutoff placed against the real distribution.** Derive the floor from the
rules, state it in the module next to the constant, and set the cutoff where
it can actually reject. If the honest answer is that the gates are the filter
and the score only ranks, then say that and remove the cutoff rather than
keeping a threshold that has never rejected anything.

**Rescoring uses the same input as scoring.** `reevaluate` loads the stored
description, or refuses when it cannot, rather than scoring a job with the
description blanked.

**Enrichment either fires or goes.** It requires a description under a short
length and an ATS host, but the ATS sources already carry long descriptions
and the thin sources have non-ATS URLs, so the conditions are close to
mutually exclusive. Both tests exercising it use a source with no importer.
Either the condition is corrected so it fires on the postings that need it, or
the step is removed and the spec records that it never ran.

**A run summary reports what was used, not what was asked for.** The
scheduled aggregate stores the requested Apify count rather than the count
actually passed to the actor, so once spec 035 clamps an out-of-range value
the record disagrees with what happened. Same class as the score fields this
spec already covers: a number in a summary that does not describe the run it
summarises. Found while reviewing spec 035 (PR #39), where fixing it would
have been a behaviour change to the run record under a security spec's
trailer.

**A scoring version stored with the score.** So a stored number carries the
policy that produced it, and a ranking across two versions is detectable
rather than silent. Shares its derivation with the screening policy version
in spec 031.

**Saturation raised or removed.** The cap sits where ranking matters most and
the best fixture posting already reaches within a few points of it, after
which ties break by list order.

## Inputs, outputs, failure modes

- Inputs: a normalized job including its description, and the scoring
  configuration.
- Outputs: one score, its signals, and the version that produced it.
- Failure modes: a job whose description was never captured cannot be
  rescored honestly, so `reevaluate` reports it as skipped with the reason
  rather than scoring it low.
- Failure mode this must not introduce: a migration that recomputes historic
  scores under today's rules and thereby destroys the record of what was
  decided at the time. Old rows keep their score and gain an unknown version.
- Removing the cutoff is an acceptable outcome of this spec. It is not a
  failure to conclude that a mechanism does nothing and delete it.

## What the implementation found

The problem statement above said the cutoff was a threshold nothing could
fail. That is true of the ATS path and wrong about the other one, and the
correction matters because it changes what the right fix is.

Anything reaching the scorer has matched an include keyword and has passed a
remote gate that tests the same patterns over the same text the remote bonus
rewards, so base plus include plus remote is unavoidable. On the ATS path the
region gate also forces the region bonus, giving a floor of 59 against a
cutoff of 55: it could not reject.

A LinkedIn result returns early from the region gate, because those searches
are region-filtered at query level, so it never earns the region bonus and its
floor is 51. Every posting the cutoff ever rejected was a LinkedIn one, and it
rejected them for the mechanism that makes them valid. So the cutoff was not
merely inert. It was a source-specific penalty that fired precisely when a
source was behaving correctly, which is worse than doing nothing, and no
single threshold can be fair to two paths whose floors differ by a bonus one
of them cannot earn.

It is removed. The gates filter and the score ranks.

Two further things surfaced while implementing, both recorded because they are
the same class of defect:

The summary's `apify_count` already disagreed with the run before spec 035's
clamp existed. A scheduled run takes its count from the stored discovery
settings while the summary reported the caller's requested value, so the
number described something that had no effect.

Migration 1 derived its column list from the live `NOTE_KEYS`, so adding the
scoring version both changed what a fresh database got at migration 1 and left
the new ALTER to run against a column that already existed. A fresh install
failed while an existing one worked. Migration 1 now records the table as it
was first created.

## Acceptance criteria

- [x] the arithmetic floor is derived by a test, so a bonus change that lifts
      it above the cutoff fails CI
      (`tests/test_scoring.py::test_the_arithmetic_floor_is_derived_from_the_rules`)
- [x] the cutoff is gone and the spec records why, above
      (`tests/test_scoring.py::test_there_is_no_score_cutoff`,
      `::test_a_realistic_posting_is_accepted_without_rigging_the_configuration`)
- [x] `reevaluate` scores with the stored description and produces the same
      number as the original pass given the same configuration
      (`tests/test_tracker_cli.py::test_reevaluate_rescores_against_the_current_config`)
- [x] `reevaluate` on a job with no stored description reports it as skipped
      (`tests/test_tracker_cli.py::test_reevaluate_without_a_stored_description_is_skipped`)
- [x] every score writer updates every score reader's field, asserted by a
      test that enumerates both sets
      (`tests/test_scoring.py::test_every_score_field_is_written_together`,
      `::test_no_reader_takes_a_field_the_writer_does_not_fill`,
      `::test_the_queue_and_the_digest_rank_by_the_same_field`)
- [x] a stored score carries its scoring version, and rows written before
      this change read as unknown
      (`tests/test_scoring.py::test_a_stored_score_carries_the_policy_that_produced_it`,
      `::test_a_score_written_without_a_version_reads_as_unknown`,
      `::test_a_migrated_database_matches_a_fresh_one`)
- [x] two postings that differ in quality above the old cap receive different
      scores (`tests/test_scoring.py::test_two_strong_postings_are_not_tied_by_a_cap`)
- [x] enrichment fires on a posting from a real source: it now runs on manual
      capture, which is the path that most needed it and the only one that
      never had it
      (`tests/test_scoring.py::test_a_manually_added_ats_url_is_enriched_before_scoring`,
      `::test_capture_can_be_told_not_to_reach_the_network`)
- [x] the Apify count is reported in the run summary as the count used, not
      the count requested
      (`tests/test_discovery.py::test_the_summary_reports_the_count_the_actor_was_given`,
      `::test_a_run_without_apify_claims_no_count`)
- [x] no real posting or company appears in a fixture (ADR-008)
- [x] All gates green on PR

## Proof / origin

The `principal-review` board, spec 028, architect, domain and screening
lenses. The `description=""` call site, the two-column split, and the
unconditional bonuses are verifiable in the tree.

## Out of scope

Changing which signals are scored or their weights, beyond what is needed to
make the cutoff meaningful. The gates themselves, which are spec 032.
