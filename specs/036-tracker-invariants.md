---
spec: 036
title: Illegal tracker states, and contract surface nothing writes
status: accepted
approved: yes
milestone: M6
depends: [004, 027]
---

# Spec 036: Illegal tracker states, and contract surface nothing writes

## Problem

`STATUSES` is a flat tuple and `set_status` validates membership only, never
the state it is moving from. Three illegal states are reachable today through
the sanctioned write path:

- `status=applied` with an empty applied date, because the date can be
  cleared independently by the generic field update.
- A resurrected job carrying the rejection reason from its previous life,
  because only the rejecting branch touches that field.
- `status=prospect` with `outreach_status=sent`, because outreach is seeded
  from the applied status and never reset when the status walks backward.

The third contradicts a stated invariant. The outreach axis is documented as
orthogonal to the pipeline status; it is coupled in one direction and never
decoupled in the other.

Alongside that, several documented behaviours have no implementation, which
is the same failure class as the nine CLI verbs that went missing between two
specs that each assumed the other owned them:

- ADR-003 states that every mutating CLI command refreshes the CSV export.
  The export is reached only from the export command.
- ADR-003 claims enforced status transitions, which spec 027 explicitly
  declined to implement.
- `manual_reject` is a database column, a response field, and a published
  contract field with no writer anywhere.
- Contacts link to jobs by fuzzy text match with no foreign key, so editing a
  job title silently breaks the link.

Found by the `principal-review` board (spec 028), domain and architect
lenses.

## Scope

**One transition contract, stated here rather than in two places.** This
originally asked for legal predecessors that `set_status` enforces. That was
built, run, and rejected during implementation, and the reasoning is under
"What the implementation decided" below. The contract as approved and built:

- An unknown status is refused, by `set_status`, as it always was.
- Every pair of known statuses is a legal move. The pipeline describes an
  intention and the world does not follow it, so a forward jump is a real
  event rather than an error.
- A move is not free: it clears what it invalidates, which is the next
  paragraph, and that is where the enforcement this spec wanted actually
  lives.

Amended after review pointed out that requiring a predecessor table here
while the implementation section said every pair is legal left the approved
behaviour ambiguous (review finding on PR #44).

**Fields that belong to a status move with it.** Entering applied requires an
applied date. Leaving rejected clears the rejection reason. Walking backward
past applied resets the outreach axis, or the invariant is rewritten to say
outreach is not orthogonal. One of those two must happen; the current state
is that the documentation and the code disagree.

**Contacts reference jobs by identity.** A real key rather than a fuzzy title
match, with the existing links migrated and unmatched ones reported rather
than dropped.

**Documented behaviour is made true or removed.** For each of the four items
above: implement it, or delete the claim and the dead surface. `manual_reject`
either gets its writer or leaves the schema, the response model, and the
published contract together.

## Inputs, outputs, failure modes

- Inputs: tracker rows and the CLI and API verbs that mutate them.
- Outputs: the same verbs, refusing illegal moves with a message naming the
  transition.
- Migration: existing rows may already be in states this spec forbids. They
  are reported by a check command and left alone. A migration that silently
  rewrites somebody's job search to satisfy a new invariant is worse than the
  inconsistency.
- Failure mode this must not introduce: a transition table so strict the
  operator cannot correct a mistake. Every status must be reachable from a
  wrong one, because the most common real edit is fixing a misclick.
- Removing `manual_reject` is a contract change and takes the generated
  client with it, which is why it is named here rather than done quietly.

## What the implementation decided

**The transition table is not built, deliberately.** It exists as a function
that permits everything, with the reasoning in
`services/api/src/harrier/tracker/transitions.py`.

A predecessor table was written and run first. It refused only forward skips,
which looked free, until the suite showed what a forward skip is: a recruiter
approaching you about a job you never applied to moves a row from `prospect`
to `interviewing` in one step, and that is the good outcome rather than a
misclick. So does a referral skipping the tailored CV. Nearly every forward
jump in this pipeline is a real event, because the pipeline describes an
intention and the world does not follow it. This spec names that failure
itself: a table so strict the operator cannot correct a mistake.

And it would have closed none of the three defects above. Every one of them
is an illegal *state* reachable through the generic field update, not an
illegal *move*, and `update_fields` never consults a predecessor table. The
invariants are enforced on the row instead, on both write paths.

Akin can overrule this: the function every caller already routes through is
where a refusal would go.

**A write is refused only when it introduces a breach.** Refusing every write
to a row that already breaks a rule would make rows written before these
rules unrepairable, which is the opposite of what this spec asks for.

**The outreach axis is reset rather than the orthogonality claim removed.**
The two axes really do move independently within a stage, so the claim is
worth keeping and the code now honours it.

**The ADR-003 export claim is corrected, not implemented.** Implementing it
would rewrite the whole tracker to record one decision and put a second copy
of the truth beside the database ADR-003 chose.

**`manual_reject` is removed rather than given a writer.** A rejection reason
already records who decided and why.

## Acceptance criteria

Proving symbols in `services/api/tests/test_tracker_invariants.py` unless
another file is named.

- [x] every legal transition and a representative illegal one has a test
      (`::test_every_status_is_reachable_from_every_wrong_one`,
      `::test_an_unknown_status_is_still_refused`). No transition is illegal;
      see above.
- [x] applied cannot be set without an applied date, by any write path
      including the generic field update
      (`::test_applied_cannot_lose_its_date_through_the_generic_update`,
      `::test_the_breach_is_named_the_same_way_wherever_it_is_found`)
- [x] leaving rejected clears the rejection reason
      (`::test_leaving_rejected_clears_the_rejection_reason`,
      `::test_a_rejection_reason_cannot_be_added_to_a_live_job`)
- [x] moving backward past applied resets the outreach axis
      (`::test_walking_back_past_applied_resets_the_outreach_axis`,
      `::test_outreach_survives_a_move_that_stays_at_or_above_applied`,
      `::test_a_planned_outreach_is_not_a_claim_that_it_happened`)
- [x] a contact stays linked to its job after the job title is edited
      (`::test_a_contact_stays_linked_to_its_job_after_the_title_is_edited`).
      Links carry a `job_id`; the text stays for display and for links that
      resolve to nothing
      (`::test_a_link_to_an_untracked_job_is_kept_and_reported`,
      `::test_the_backfill_links_old_records_and_drops_none`)
- [x] the ADR-003 export claim is either implemented or removed, and a test
      asserts whichever is chosen
      (`::test_a_status_change_does_not_write_the_csv_export`)
- [x] `manual_reject` is absent from the schema, the response model, and the
      generated client together
      (`::test_manual_reject_is_gone_from_every_surface_together`,
      `::test_a_database_at_the_previous_version_loses_the_dead_column`)
- [x] a check command reports pre-existing illegal rows and changes nothing
      (`::test_check_reports_pre_existing_rows_and_changes_nothing`,
      `::test_check_is_quiet_and_succeeds_on_a_clean_tracker`,
      `::test_a_row_that_already_breaks_a_rule_can_still_be_repaired`,
      `::test_check_reports_an_unresolved_contact_link`).
      `harrier check --link-contacts` is the one thing it changes, and only
      when asked.
- [x] no tracker content appears in a test name, fixture, or message
      (ADR-008). Every fixture here is an invented company. Limitation: this
      is a property of the diff rather than something a test asserts.
- [x] All gates green on PR

## Proof / origin

The `principal-review` board, spec 028. Each reachable illegal state was
traced to the specific write path that permits it, and each dead symbol to
its absent caller.

## Out of scope

Scores and the two score columns, which are spec 033. Rewriting the tracker
schema into typed columns: this spec constrains transitions, not
representation.
