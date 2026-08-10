---
spec: 024
title: Cutover and archive
status: shipped
approved: yes
milestone: M5
depends: [022]
---

# Spec 024: Cutover and archive

## Problem

Split from spec 022, which covered verification and the cutover event
together. Verification is read-only, repeatable, and shipped. This is the
other half: one irreversible sitting that stops the system running Akin's
job search and starts a different one in its place.

It is separated because the two need different gates. Verification needed a
code review. This needs a person deciding that parity is reached, on a day
they can watch the first scheduled run land.

## Scope

Cutover plan phases 3 and 4 (docs/cutover-plan.md), unchanged in substance:

- quiesce: unload the three old plists
- snapshot the old tracker, state, and gmail event log
- final migration refresh: tracker rows added during the dual run, the
  description cache, the gmail seen-state, and a refresh of the discovery
  seen-state already migrated in phase 2a
- verify: row counts, schedule status, one dry-run discovery and digest
- go live: harrier schedule install, watch the next scheduled run
- one-week fallback window, then archive the old repo read-only

## Preconditions, to be met before this is approved

- `harrier parity status` reports every checklist item checked or waived.
- Phase 2a has been performed: the discovery seen-state is migrated, so the
  diff compares screening rather than declining to. This is a precondition
  of the dual-run period, not of cutover; listing it here as a cutover step
  made these preconditions circular (review finding on PR #19).
- At least one `harrier parity diff` over a shadow run is clean, including a
  weekday morning so the Apify path and its cost gate are exercised.
- The `.env` defect in the old repo is fixed or knowingly accepted: a value
  spanning a line break makes the shell wrapper exit 127 under `set -e`,
  which is why the old digest stopped running. It matters here because the
  fallback window depends on the old system still working.

## Acceptance criteria

`shipped` here means the tooling shipped. **The cutover has not happened.**
`harrier cutover preflight` currently reports blockers and the sequence
refuses to execute, which is the correct state until the event criteria
below are satisfied.

Criteria for the tooling, which is what this spec delivers:

- [x] preflight blocks on an undecided parity checklist, an empty tracker,
      old jobs already gone, and a malformed old .env, naming the line
      (test_an_undecided_checklist_blocks, test_an_empty_tracker_blocks,
      test_jobs_already_gone_blocks,
      test_a_malformed_env_line_blocks_and_names_the_line)
- [x] a dry run is the default and issues no bootout
      (test_a_dry_run_touches_nothing)
- [x] execution is refused when preflight is blocked, and refused again
      when the attestations have not been made
      (test_executing_without_a_clear_preflight_is_refused,
      test_executing_without_attestation_is_refused)
- [x] a full execution quiesces, snapshots outside the repo, verifies, and
      installs, writing its record to data/
      (test_a_full_execution_quiesces_snapshots_verifies_and_installs)
- [x] a refused unload stops before any data is copied, rolls back what it
      already stopped, and reports a rollback that itself fails; an
      already-unloaded job is not a failure
      (test_a_refused_unload_stops_before_the_data_is_touched,
      test_a_failed_unload_rolls_back_what_was_already_stopped,
      test_a_rollback_that_itself_fails_is_reported,
      test_an_already_unloaded_job_is_not_a_failure)
- [x] a blocked dry run reports its blockers rather than reading as clean
      (test_a_blocked_dry_run_reports_the_blockers_and_is_not_ok,
      test_a_clear_dry_run_is_ok)
- [x] a failing schedule install still writes the record of the
      irreversible steps already taken
      (test_a_failing_install_still_writes_the_record)
- [x] All gates green: `just check`, run by the CI workflow's Python and
      TypeScript jobs

Criteria for the cutover event itself, which only Akin can satisfy:

- [ ] every parity row checked or explicitly waived
- [ ] one post-migration shadow diff clean over a full weekday cycle
      including an Apify morning
- [ ] old plists unloaded, new plists live, first scheduled run observed
- [ ] fallback window documented with its expiry date
- [ ] old repo tagged archived and its plists removed

## Proof / origin

docs/cutover-plan.md phases 3 and 4. Proving file:
services/api/tests/test_cutover.py.

State when this shipped, from `harrier cutover preflight` against the real
machine: two blocking checks. The parity checklist stood at 0 of 96 decided,
and the old repo's .env has a value spanning a line break at line 20, which
is the defect that has kept the old digest from running. The tracker check
passed at 701 jobs and all three old jobs were loaded. So the tooling exists
and correctly refuses; the cutover has not happened.

## Stated changes from the plan

- The cutover record goes to `data/cutover/<stamp>.md`, not
  `docs/cutover-log.md`. A dated record of a real job search with row counts
  is operational state about a person and does not belong in a public
  repository (ADR-008). The plan is amended rather than followed.
- The migration refresh is not automated here. Phase 2a already migrates the
  seen-state, and the remaining refresh is `harrier migrate-legacy` against
  the snapshot, which exists and is better run with eyes on it than buried
  inside a sequence that has just unloaded the old scheduler.

Honest limitations: launchctl is not invoked in tests, so quiesce is proven
for the command lines it builds and its error handling, not for launchd's
behavior. Nothing here verifies that the new system is correct, only that
the switch is performed safely and in order; correctness is what the parity
checklist and the dual-run period are for.

## Out of scope

Anything spec 022 covers. Deleting the old repo: it stays on disk,
read-only, and its history stays private forever regardless of archival
(docs/privacy-plan.md).
