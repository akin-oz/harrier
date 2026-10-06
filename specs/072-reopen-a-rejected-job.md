---
spec: 072
title: A rejected job can be reopened from the tracker page
status: accepted
approved: yes
milestone: M8
depends: [036, 042, 015]
---

# Spec 072: A rejected job can be reopened from the tracker page

## Problem

A rejection is not always final. The batch evaluator (spec 015,
`services/api/src/harrier/offers/batch.py`) rejects prospects on its own
with an `ai-evaluation:` reason. When a recruiter from that company later
writes about the same role, the operator needs the row back in the
pipeline.

The tracker already permits this. `transition_allowed`
(`services/api/src/harrier/tracker/transitions.py`) refuses no move, and
`fields_a_move_clears` clears `rejection_reason` when a row leaves
`rejected` (spec 036). The CLI reaches it: `harrier shortlist <id>` on a
rejected row works today.

The tracker page does not. `JobActions.tsx` sets `closed` for a rejected
row and disables every status button on it. No verb lists `rejected` in
its `from`, so the row has no primary action either. The operator's
daily driver offers no way back.

## Scope

`apps/web/src/features/tracker/JobActions.tsx`, its test in
`apps/web/src/pages/tracker/TrackerPage.test.tsx`, and one test in
`services/api/tests/test_offers.py`. No API, contract, schema or CLI
change.

## Behavior

| # | Behavior |
|---|---|
| R1 | **Reopen is the primary action on a rejected row.** A row with status `rejected` shows a primary button labelled `Reopen`. It sends the existing `shortlist` verb. The row becomes `shortlisted`. |
| R2 | **The other forward verbs are live on a rejected row.** Under `More`, `Request CV`, `Applied` and `Interviewing` are enabled on a rejected row and send their existing verbs. A recruiter who writes after a rejection can move the row straight to `interviewing`. |
| R3 | **Reject and Apply stay disabled on a rejected row.** Rejecting again would overwrite the recorded reason with nothing new. Apply needs an open row; the operator reopens first. |
| R4 | **Reopening clears the reason.** After R1 or R2 the row's `rejection_reason` is empty. This is spec 036 behavior through `fields_a_move_clears`, not new code; this spec adds a test that drives it from the page. |
| R5 | **Reopen lands on shortlisted, not prospect.** `evaluate_prospects` reads only `prospect` rows. A reopened row at `prospect` would be evaluated again on the next `--refresh` run and could be auto-rejected a second time. `shortlisted` is a human decision the batch never touches. |
| R6 | **No new verb.** `STATUS_BY_VERB` is unchanged, so the spec 042 test that the browser's verbs match the CLI's stays as it is. |

## Acceptance criteria

| Criterion | Proof |
|---|---|
| R1 a rejected row shows Reopen and it sends `shortlist` | `TrackerPage.test.tsx`: `a rejected row offers Reopen, which shortlists it` |
| R2 Interviewing is enabled on a rejected row and sends `interviewing` | `TrackerPage.test.tsx`: `a rejected row can move straight to interviewing` |
| R3 Reject and Apply are disabled on a rejected row | `TrackerPage.test.tsx`: `a rejected row cannot be rejected again or applied to` |
| R4 the reason is gone after reopening | existing `services/api/tests/test_tracker_invariants.py::test_leaving_rejected_clears_the_rejection_reason` |
| R5 a reopened row is not re-evaluated | `services/api/tests/test_offers.py::test_a_reopened_row_is_not_evaluated_again`: a row rejected then shortlisted is skipped by `evaluate_prospects` |

- [x] each new test fails with its behavior removed: checked by removing each behavior in turn (4 mutants, all failed a test)
- [x] `pnpm type-check`, `pnpm lint`, `just check` green

## Data and privacy

Tests use synthetic companies. No real rejection reasons in fixtures.

## Limitations

- **The audit entry stays.** `data/evaluations/audit.jsonl` still records
  the auto-rejection. That is the audit's job; nothing marks it reversed.
- **The evaluation report stays.** A later `--refresh` run does not
  touch a shortlisted row (R5), but the old report file still says skip.

## Proof / origin

Operator request of 2026-10-04: a recruiter message arrived for a role the
batch evaluator had rejected, and the tracker page offered no way to
reopen it. `closed` in `JobActions.tsx` as of commit fb49497.

## Out of scope

Postings rejected by screening never reach the tracker; `Add job` on the
tracker page (spec 042) already adds those by hand. Bulk reopen. A
reopen reason field.

## Amendment: Interviewing is the company's invitation (2026-10-06)

R2's recruiter path is unchanged in effect: a rejected row can still move
straight to `interviewing` from the browser. The control under More is now
named **Interview invite** and records the company's `interview_invited`
outcome through the outcome route, because an interview is something the
company did (specs 079, 080). The `interviewing` verb itself is recorded the
same way by the domain. Proof: `a rejected row can move straight to
interviewing` in `TrackerPage.test.tsx`, updated to click Interview invite
and assert the outcome request.
