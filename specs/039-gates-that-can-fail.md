---
spec: 039
title: Gates that can fail: approval, and the parity checklist
status: accepted
approved: yes
milestone: M6
depends: [022, 028]
---

# Spec 039: Gates that can fail: approval, and the parity checklist

## Problem

Two of this project's governance mechanisms cannot say no, and one of them is
the mechanism the whole scheme rests on.

**The spec gate reads approval from the branch requesting it.** CI resolves a
commit's `Spec: NNN` trailer by grepping the spec file as it exists on the
pull request branch, so a pull request can approve its own spec and pass. The
most recent merge shows the shape: the approval commit is an ancestor of the
implementing commit inside the same pull request. In practice this repository
has one author who is also its approver, so the gate has never had a
disagreement to detect, which is precisely why nobody noticed it cannot
detect one.

**The parity checklist has never produced a decision.** Ninety-seven items,
none ticked, none waived. The status command has reported incomplete since it
shipped, so its signal is indistinguishable from noise, and cutover has four
preconditions of which this is one.

This is not a bookkeeping complaint. It is the mechanism that would have
caught the nine missing CLI verbs: the parity matrix did list all twelve, the
checklist did generate the items, and nothing was wired to contradict the
assumption that they existed. Coverage by enumeration cannot find a gap. Only
a contradiction can.

Found by the `principal-review` board (spec 028), architect lens.

## Scope

**Approval is resolved from a source the pull request cannot edit.** The base
branch, or a signed marker, or an out-of-band record. The requirement is only
that a branch cannot grant itself the approval it is asking for.

**Self-approval is legitimate here and stays possible.** One author is one
author, and pretending otherwise would be theatre. What changes is that the
approval must exist before the work is proposed, rather than arriving inside
the proposal. The gate stops being a rubber stamp without pretending there is
a second person.

**Checklist items resolve against something executable.** Each item is either
tied to a check that can pass, or explicitly waived with a reason. An item
that can only ever be ticked by hand is a note, and it is labelled as one so
the count means something.

**The status command reports the three populations separately**: verified by
a check, waived with a reason, and outstanding. A single incomplete number
over ninety-seven items carries no information.

## Inputs, outputs, failure modes

- Inputs: commit trailers, spec files, the parity matrix, the checklist.
- Outputs: a CI verdict that can be no, and a checklist status that can
  distinguish progress from stasis.
- Failure mode this must not introduce: a gate that blocks the author from
  working on their own project. The approval must be cheap to grant and hard
  to grant accidentally, which is a different property from being hard to
  grant.
- Failure mode this must not introduce: checklist items auto-ticked by a
  check that asserts nothing, which would convert an honest zero into a
  dishonest ninety-seven. Every automated item names the assertion it runs.
- The honest limitation: a single-author repository cannot have independent
  approval, and no mechanism here creates one. What it can have is approval
  that is recorded before the work rather than alongside it.

## What the implementation decided

**Approval is resolved from the base tree, with one exemption.** A commit
that touches only `specs/` may reference a spec not yet approved on the base,
because that is how an approval reaches the base at all. A commit that
touches a spec and anything else is a proposal, so approving and implementing
in one commit is refused. The gate's logic moved out of the workflow into
`scripts/spec_gate.py` so it could be tested against real repositories: a
gate tested against a mock of the thing it gates proves nothing, and the
defect being closed was precisely that it read the wrong tree.

**Five items are automated, not ninety-seven.** Every other item is reported
as manual and says so. Inventing checks to move the number would be the
failure this spec names, and a check that cannot fail is worse than an
unticked box, because it looks like verification.

**The status command exits non-zero only for something that can be wrong**: a
check that failed, a tick with no reason, or a decision recorded against an
item the matrix no longer carries. Ninety-two unread notes are not a failure,
and exiting non-zero for them forever is the noise this replaces.

## Acceptance criteria

- [x] a pull request that adds or flips its own approval fails the gate
      (`services/api/tests/test_spec_gate.py::test_a_branch_cannot_approve_the_spec_it_is_implementing`,
      `::test_flipping_approval_inside_the_pull_request_is_refused`,
      `::test_approving_and_implementing_in_one_commit_is_refused`)
- [x] a pull request whose spec was approved on the base branch passes
      (`::test_a_spec_approved_on_the_base_passes`,
      `::test_a_governance_commit_may_carry_its_own_approval`)
- [x] the gate's failure message says which spec and where approval must live
      (`::test_the_message_says_which_spec_and_where_approval_must_live`)
- [x] every checklist item is classified as automated, waived, or manual, and
      the classification is derived rather than hand-maintained
      (`services/api/tests/test_parity_checklist.py::test_every_item_is_classified_exactly_once`,
      `::test_the_classification_is_not_read_from_the_checklist`,
      `::test_every_registered_check_names_a_real_matrix_item`)
- [x] an automated item names the check it runs, asserted by a test that a
      check with no assertion cannot tick an item
      (`::test_an_automated_item_names_the_check_it_runs`,
      `::test_no_check_can_pass_when_its_subject_is_absent`)
- [x] the status command reports the three populations separately and exits
      non-zero only when an automated item fails
      (`::test_a_check_that_fails_is_reported_and_not_counted_as_verified`;
      the exit rule also covers an unreasoned tick and a retired item, which
      are decisions that are wrong rather than absent)
- [x] a waiver without a reason is refused
      (`::test_a_waiver_without_a_reason_is_refused`,
      `::test_an_unticked_item_is_not_a_missing_waiver`,
      `::test_the_committed_checklist_records_no_unreasoned_tick`)
- [x] All gates green on PR

The honest limitation stands and is worth restating: a single-author
repository cannot have independent approval, and nothing here creates one.
What it now has is approval recorded before the work rather than alongside
it, and ninety-two items that say plainly that a human has not looked at them
yet.

## Proof / origin

The `principal-review` board, spec 028. The workflow's grep against the
checked-out branch and the checklist's ninety-seven-to-zero state are both
verifiable in the tree.

## Out of scope

Requiring a second human reviewer. Changing what the parity matrix contains
or how it is generated, beyond the classification this adds.
