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

- Seven pills, in this order: `hybrid`, `onsite`, `closed`,
  `missing stack`, `location`, `language`, `rejected by company`
  (the last three added by the amendment below). Clicking a pill
  submits the rejection immediately with that exact string as the
  reason. One click, no confirm step: the pill is the confirmation, and
  Cancel remains for a mis-press before the click.
- A final pill, `other…`, reveals the existing free-text input with its
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

- Pressing Reject shows the seven pills and `other…`; clicking `hybrid`
  posts the status change with `verb: "reject"`, `reason: "hybrid"`, and
  no further confirmation step.
- Each of the seven pills submits its exact lowercase label as the
  reason.
- Clicking `other…` reveals the text input; typing a reason and
  confirming posts it, unchanged from today's behavior.
- While the picker is open, the row's other verbs are not in the
  document (the existing mid-decision test keeps passing).
- Cancel closes the picker without posting.
- In a browser, `other…` shows in the muted text color and the pills keep
  the ordinary one ("other…'s muted color outranks the shared button
  rule" in `TrackerPage.test.tsx`, which fails without the fix). See
  "Amendment: `other…` is muted in a browser too" below.
- `pnpm type-check` and `pnpm lint` pass; the vitest suite passes.

## Proof / origin

The friction and the inconsistent spellings are observable in this
machine's tracker rows, where the same hybrid rejection appears under
several phrasings. The four pill labels are the operator's own list of
their most frequent reasons. The reject flow being changed is
`JobActions.tsx` (spec 042 built the verb row; the reason input arrived
with it), and the mid-decision rule this preserves is the review finding
on PR #41.

## Amendment: three more pills (2026-10-04)

The operator added three reasons they now give often enough to want one
click: `location`, `language`, and `rejected by company`. The last one
records that the company declined the candidate, rather than the
operator declining the job.

They follow the original four, so the existing order and the muscle
memory for it do not move. Everything else in this spec holds for them:
lowercase stored values, one click, shortcuts rather than an enum.

`rejected by company` is recorded in `rejection_reason` like the others.
The tracker has one `rejected` status for both directions, and this
amendment does not split it.

Proof: `every pill submits its exact lowercase label as the reason` in
`TrackerPage.test.tsx` iterates over all seven.

## Out of scope

- Validating or migrating existing `rejection_reason` values.
- An enum in the contract or tracker schema; the reason stays a free
  string everywhere outside this picker.
- Pills anywhere else (CLI, inbox, apply page).
- Analytics or grouping over rejection reasons; the pills merely make
  future grouping possible by making the frequent values consistent.

## Migration

None.

## Amendment: the pill list is spec 080's (2026-10-06)

`rejected by company` is no longer a pill. It recorded a company's verdict
through the candidate's Reject control, which is the confusion spec 079
exists to end. A company's response now has its own control, Company
replied, in the same one-click pattern this spec established (spec 080).

The six remaining pills keep this spec's exact strings, so
`rejection_reason` stays groupable across old and new rows. Each pill also
sends a reason code alongside its text. The text stays a free string; the
code is a separate field the contract declares (spec 080), which supersedes
the "no enum in the contract" line under Out of scope for that field only.

The pill list, its codes, and the `other…` select are specified in spec 080
from here on. Proof: `every pill submits its exact lowercase label as the
reason` in `TrackerPage.test.tsx` still iterates over every pill, now six,
and "each pill sends its code and text" in `TrackerPage.test.tsx` covers the
codes.

## Amendment: `other…` is muted in a browser too (2026-10-06)

This spec's implementation (commit e19c604) gave `other…` the muted text
color, `--color-text-secondary`, and left the pills in the ordinary text
color. It is the visual half of what Behavior says of free-form reasons:
they remain possible, and they stop being the default path. This spec never
said so, and until this amendment no browser showed it.

- **Why no browser showed it.** `.job-actions button` sets the color of
  every control in the block, and it outranks the bare
  `.job-actions__pill-other` in `apps/web/src/features/tracker/JobActions.css`.
  In a browser in demo mode, `other…` computed `--color-text`, the pills'
  color, while Cancel, whose rule was already scoped, computed
  `--color-text-secondary`. jsdom does not rank rules by specificity, so a
  rendered test sees the muted color and passes. Spec 080 found this in its
  review of the merged range and left it to its own change.
- **The fix.** The rule is scoped under `.job-actions button`, as Cancel's
  is, so it outranks the shared rule.
- **Whose rule this is.** The amendment above hands the pill list, the codes
  and the `other…` select to spec 080. How `other…` looks stays with this
  spec, whose implementation set it.

Files: `JobActions.css` and `TrackerPage.test.tsx`, both already in this
spec's scope.

Proof: "other…'s muted color outranks the shared button rule" in
`TrackerPage.test.tsx`. It opens Reject and finds the rule that mutes
`other…` by what it declares; that rule must match `other…` and no pill. It
then reads the cascade from the stylesheet as parsed, and fails if any rule
that styles the takeover's buttons at rest, or the buttons of the reason
form behind `other…`, loses to `.job-actions button`. Without the fix it
names `.job-actions__pill-other`; without the class on `other…`, it finds
no muted rule. Checked in a browser in demo mode as well: `other…` and
Cancel compute `--color-text-secondary`, the pills `--color-text`, and a
hovered `other…` shows the ordinary accent, not the danger color.

Limitation: as in spec 047's amendment of the same date, the test compares
selectors rather than computed styles, because jsdom cannot rank rules.
