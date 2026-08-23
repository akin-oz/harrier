---
spec: 056
title: Rejecting a job offers the frequent reasons as one-click pills
status: accepted
approved: yes
milestone: M8
depends: [042]
---

# Spec 056: Rejecting a job offers the frequent reasons as one-click pills

## Problem

Rejecting a job in the web tracker requires typing a free-text reason
into an input and confirming (`apps/web/src/features/tracker/
JobActions.tsx`). The operator rejects many jobs for the same four
reasons: the role is hybrid, it is on-site, the posting closed, or the
stack does not match. Typing them each time is friction at exactly the
moment the operator is triaging a batch, and free text spells the same
reason many ways ("hybrid", "Hybrid role", "hybrid :("), which makes
`rejection_reason` useless for grouping later.

## Scope

- `apps/web/src/features/tracker/JobActions.tsx` and its stylesheet: the
  reject flow's reason entry.
- `apps/web/src/pages/tracker/TrackerPage.test.tsx`: the tests that
  exercise the reject flow.

Frontend only. The status endpoint already takes `reason` as a free
string, so the contract, the API, and the tracker schema do not move.

## Behavior

Pressing Reject on a row replaces the bare text input with a reason
picker:

- Four pills, in this order: `hybrid`, `onsite`, `closed`,
  `missing stack`. Clicking a pill submits the rejection immediately
  with that exact string as the reason. One click, no confirm step: the
  pill is the confirmation, and Cancel remains for a mis-press before
  the click.
- A fifth pill, `other…`, reveals the existing free-text input with its
  Confirm button, exactly as today. Free-form reasons remain possible;
  they stop being the default path.
- Cancel closes the picker (and the revealed input) without a change,
  as today.
- The mid-decision rule is unchanged: while the picker or the input is
  open, the row's other verbs stay hidden (review finding on PR #41,
  already pinned by an existing test).
- A refusal from the API is shown verbatim, as today.

The pill strings are the stored reason values, lowercase, exactly as
listed. They are frequent-case shortcuts, not an enum: the API keeps
accepting any string, the CLI is untouched, and nothing validates
existing rows against the list.

## Failure modes

- **Pill click while a mutation is in flight**: pills are disabled like
  the existing buttons (`busy`), so a double-click cannot submit twice.
- **The API refuses the transition** (already-rejected row, race with
  another surface): the refusal message renders verbatim and the picker
  stays open; no state is lost.
- **Empty free text after choosing `other…`**: Confirm stays disabled,
  exactly as the input behaves today.

## Acceptance criteria

Proof lives in `apps/web/src/pages/tracker/TrackerPage.test.tsx`.

- Pressing Reject shows the four pills and `other…`; clicking `hybrid`
  posts the status change with `verb: "reject"`, `reason: "hybrid"`, and
  no further confirmation step.
- Each of the four pills submits its exact lowercase label as the
  reason.
- Clicking `other…` reveals the text input; typing a reason and
  confirming posts it, unchanged from today's behavior.
- While the picker is open, the row's other verbs are not in the
  document (the existing mid-decision test keeps passing).
- Cancel closes the picker without posting.
- `pnpm type-check` and `pnpm lint` pass; the vitest suite passes.

## Proof / origin

The friction and the inconsistent spellings are observable in this
machine's tracker rows, where the same hybrid rejection appears under
several phrasings. The four pill labels are the operator's own list of
their most frequent reasons. The reject flow being changed is
`JobActions.tsx` (spec 042 built the verb row; the reason input arrived
with it), and the mid-decision rule this preserves is the review finding
on PR #41.

## Out of scope

- Validating or migrating existing `rejection_reason` values.
- An enum in the contract or tracker schema; the reason stays a free
  string everywhere outside this picker.
- Pills anywhere else (CLI, inbox, apply page).
- Analytics or grouping over rejection reasons; the pills merely make
  future grouping possible by making the frequent values consistent.

## Migration

None.
