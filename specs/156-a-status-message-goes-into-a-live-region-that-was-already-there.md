---
spec: 156
title: A status message goes into a live region that was already there
status: shipped
approved: yes
milestone: M6
depends: []
---

# Spec 156: A status message goes into a live region that was already there

## Problem

The web app tells the operator about quiet outcomes through `role="status"`
regions: "Brief saved.", "Saved. Every operation reads this description.",
"Lost the log stream", a digest's outcome, a sign-in message. A status
region is a polite live region. Assistive technology speaks a change to its
content.

The WAI-ARIA authoring guidance and MDN's "ARIA live regions" page both say
the region must already be in the document when its content changes.
Whether a region inserted together with its text is announced depends on
the browser and screen reader pair. So a message that arrives that way may
be shown and never spoken.

A survey of every `role="status"` in `apps/web/src` on 2026-10-17 found 22
regions. Seven are always rendered and change only their text. Fifteen
mount the region with its message. Two of the seven hide their region with
`display: none` while empty, which takes it out of the accessibility tree
the same way. TracksPage's hiding rule and BackupsSection's computed role
were found while building; see the amendments below.

Who it hurts: an operator using a screen reader, who saves a brief or loses
a log stream and hears nothing.

The concern was raised by the local fsd-reviewer during the post-merge
review of spec 149 (PR #387) and declined there as app-wide, out of that
spec's scope.

### The survey

Mounted with the message (each changes under this spec):

| File | Region | Mounted when |
|---|---|---|
| `features/brief/BriefPanel.tsx` | `brief-panel__muted` | `save.isSuccess` |
| `features/description/DescriptionPanel.tsx` | `description-panel__note` | `notice !== null`, inside the stored-text branch that itself mounts on save |
| `features/auth/SignInForm.tsx` | `sign-in-form__message` | `message !== null` |
| `features/tracker/AddJob.tsx` | `add-job__outcome` | `outcome !== null` |
| `features/tracker/JobActions.tsx` | `job-actions__failure` | `failure !== null` (focus also moves to it) |
| `features/config/LineListEditor.tsx` | `config-unrouted` | routing has unrouted lines |
| `features/schedule/ScheduleForm.tsx` | `schedule-form__state` in `Saved` | `savedOnce` |
| `pages/settings/BackupsSection.tsx` | `settings-result`, as `role={failed ? "alert" : "status"}` | a run has finished and printed a line |
| `features/runs/RunPanel.tsx` | `run-panel__disconnected` | `disconnected` |
| `pages/apply/ApplyPage.tsx` | `apply-page__muted` | `disconnected` |
| `pages/inbox/InboxPage.tsx` | `inbox-page__muted` | `disconnected` |
| `pages/operations/DataChecksSection.tsx` | `ops-outcome` | `link.isSuccess` |
| `pages/operations/DigestSection.tsx` | `ops-outcome` | `outcome !== null` |
| `pages/operations/EligibilitySection.tsx` | `ops-outcome` "Eligibility report" in `ReportView` | `shown !== null` |
| `pages/operations/RunOutput.tsx` | `ops-run__state` | `run !== null` (the component returns null before) |

Always mounted, but hidden while empty:

| File | Region | How |
|---|---|---|
| `features/tracks/ArchiveTrack.tsx` | `archive-track__message` | `.archive-track__message:empty { display: none; }` in its stylesheet |
| `pages/tracks/TracksPage.tsx` | `tracks-page__notice` | `.tracks-page__notice:empty { display: none; }` in `TracksPage.css` |

Already following the rule (unchanged): `features/config/KindFrame.tsx`,
`features/config/ImportConfig.tsx`, `features/tracks/AddTrack.tsx`,
`features/host-commands/CopyCommand.tsx`,
`pages/settings/ExportSection.tsx`.

## Scope

1. The rule, stated once, for every `role="status"` region in
   `apps/web/src`.
2. The seventeen regions above brought under it.
3. One test per changed component proving its region is in the document
   before the message, and that the message lands in that same element.

## Behavior

### The rule

- A `role="status"` region is rendered whenever the component or form that
  owns its message is rendered, whether or not there is a message. It is
  not inside a condition that depends on the message, and it is not inside
  a child component or branch that mounts at the same moment as the
  message.
- Only the region's content changes. No message replaces the region
  element itself: the element that holds the message is the element that
  was there, empty, before it.
- Content inside the region may mount and unmount. A styled box (a border,
  padding, a background) belongs to an element inside the region, so the
  empty region shows nothing. The region element itself carries no box
  styling.
- A region is never hidden while empty with `display: none`,
  `visibility: hidden` or the `hidden` attribute.
- An existing accessible name stays: the eligibility region keeps
  `aria-label="Eligibility report"`.

### Each changed component

What the operator sees is unchanged in every case: the same words in the
same place, at the same moment. Only the element structure changes.

- **BriefPanel.** The region sits in the brief form, empty until a save
  succeeds, then holds "Brief saved.".
- **DescriptionPanel.** The region sits in the panel's loaded view, outside
  the stored-text branch, so it is present while the paste box is open and
  holds the save note when the stored text appears. "Replace" empties it, as
  it clears the note today. The loading and error views do not render it:
  the note can only follow a save, which needs the loaded view.
- **SignInForm.** The region sits in the form for both steps and holds the
  current message.
- **AddJob.** The region sits in the add form, rendered when the form opens,
  and holds the outcome.
- **JobActions.** The region is always rendered. The failure paragraph
  inside it keeps its `ref` and `tabIndex={-1}`, and focus still moves to it
  when a failure appears, as `TracksPage` already does with its notice.
- **LineListEditor.** The region is always rendered for the feeds kind,
  the only kind whose lines are checked. The bordered `config-unrouted` box
  mounts inside it when there are unrouted lines.
- **ScheduleForm.** The region sits in the form. `Saved` renders its content
  into it once the form has saved.
- **RunPanel, ApplyPage, InboxPage.** The region sits in the panel (for
  ApplyPage, the operation panel) and holds the "Lost the log stream" text
  while the stream is lost. RunPanel's bordered paragraph moves inside the
  region.
- **DataChecksSection, DigestSection.** The region sits in the section and
  holds the link outcome or the digest outcome.
- **EligibilitySection.** The region, with its name, sits in the section.
  `ReportView` renders its content into it.
- **RunOutput.** With no run, it renders its state region, empty, and
  nothing else visible. With a run, the state line fills that same region.
- **BackupsSection.** A finished run that failed keeps its `role="alert"`
  paragraph, mounted with its message as an alert is meant to be. A run
  that succeeded puts its last line in a `role="status"` paragraph that is
  always rendered in the section.
- **ArchiveTrack, TracksPage.** The `:empty { display: none; }` rules are
  removed. Each empty paragraph has no margin, so it takes no height; it
  may add one flex gap. TracksPage still moves focus to its notice.

## Failure modes

| Case | Result |
|---|---|
| Two regions on one view (Apply page: the description panel and the stream notice; Operations: a section outcome and its run state) | Each region is its own element in its own owner. Tests that asked for the only `status` on a view scope to the owner (`within`) or to the region's name. |
| A message that is set, cleared and set again to the same words | The region holds the text again. Whether a screen reader repeats identical text is the reader's behavior; this spec does not force it. |
| The owner itself mounts with its first message (a form that opens and already has an outcome) | Not possible in the surveyed components: each message follows an action taken inside its owner. A future component where it is possible moves its region up to an owner that was already rendered. |
| An empty region inside a flex container with `gap` | It may add one gap of space. Accepted; no region is hidden to avoid it. |
| A region that is also a focus target (JobActions) | The paragraph inside the region takes focus and the region announces it. A reader may speak it twice; that is the behavior `TracksPage` has today. |

## Acceptance criteria

Each test renders the owner, finds the region before the action, takes the
action, and checks that the message is in that same element (`toBe` on the
node), or, where a view holds several regions, in one of the regions that
were there before the action, with no region holding the message mounted
since (a nested region kept inside the new one would be that insertion). Each one fails with the components as they
were before this change; the other five tests named in the last criterion
fail too.

- [x] BriefPanel: the region is present and empty before a save, and holds
      "Brief saved." after one
      (`BriefPanel.test.tsx::the saved note lands in a status region that was already there`).
- [x] DescriptionPanel: the region is present while the paste box is open,
      and holds the save note after a save
      (`DescriptionPanel.test.tsx::the saved note lands in a status region that was already there`).
- [x] SignInForm: the region is present before the first send, and holds
      the message after it
      (`SignInForm.test.tsx::the sign-in message lands in a status region that was already there`).
- [x] AddJob: the region is present when the form opens, and holds the
      outcome after a save
      (`TrackerPage.test.tsx::the outcome lands in a status region that was already there`).
- [x] JobActions: the region is present before a refused action, holds the
      failure after it, and the failure paragraph has focus
      (`TrackerPage.test.tsx::a failure lands in a status region that was already there and takes focus`).
- [x] LineListEditor: the region is present before the blur, and holds the
      unrouted list after it
      (`LineListEditor.test.tsx::unrouted lines land in a status region that was already there`).
- [x] ScheduleForm: the region is present before a save, and holds the
      saved note after it
      (`ScheduleChoiceSection.hosted.test.tsx::the saved note lands in a status region that was already there`).
- [x] RunPanel: the region is present before the stream is lost, and holds
      "Lost the log stream" after
      (`RunPanel.test.tsx::a lost stream is told in a status region that was already there`).
- [x] ApplyPage: the same, for the operation panel
      (`ApplyPage.test.tsx::a lost stream is told in a status region that was already there`).
- [x] InboxPage: the same, for the watch
      (`InboxPage.test.tsx::a lost stream is told in a status region that was already there`).
- [x] DataChecksSection: the region is present before linking, and holds
      the outcome after
      (`OperationsPage.test.tsx::the link outcome lands in a status region that was already there`).
- [x] DigestSection: the region is present before a send, and holds the
      outcome after
      (`OperationsPage.test.tsx::the digest outcome lands in a status region that was already there`).
- [x] EligibilitySection: the "Eligibility report" region is present before
      a run, and holds the report after
      (`OperationsPage.test.tsx::the eligibility report lands in a status region that was already there`).
- [x] RunOutput: a section with no run renders an empty state region, and
      the run's state fills that same element
      (`OperationsPage.test.tsx::a run's state lands in a status region that was already there`).
- [x] BackupsSection: the region is present before a backup, and holds the
      finished run's line after it
      (`SettingsPage.test.tsx::a finished backup is told in a status region that was already there`);
      a failed backup is still an alert
      (`SettingsPage.test.tsx::a failed backup says no archive was written`).
- [x] ArchiveTrack and TracksPage: no stylesheet rule hides the message
      while empty. Checked in the diff of `features/tracks/tracks.css` and
      `pages/tracks/TracksPage.css`, not by a test: jsdom computes no
      stylesheet `:empty` rule a test could rely on.
- [x] Existing tests that asserted `queryByRole("status")` is null for "no
      message" assert an empty region instead, and still fail if the
      message appears
      (`RunPanel.test.tsx::a recovered stream stops saying it is disconnected`,
      `::a run the stream does not know stops the reconnecting`,
      `::an event that is not JSON is skipped, and the stream carries on`;
      `DescriptionPanel.test.tsx::replacing clears the saved note, so cancelling does not report a save`;
      `ScheduleChoiceSection.hosted.test.tsx::settings schedule section shows the store's refusal`).
      Tests that found the region to read its first message now wait for
      the words, since the region is found at once, empty
      (`message` in `SignInForm.test.tsx`, `eligibilityReport` in
      `OperationsPage.test.tsx`).
- [x] `grep -rnE 'role="status"|role=\{' apps/web/src` after the change
      lists no status region inside a `&&` or ternary on its own message,
      and no role computed between `alert` and `status`. Checked in review.
      The second pattern is there because `role="status"` alone missed
      BackupsSection.

Every file named above exists today. AddJob and JobActions are tested
through `pages/tracker/TrackerPage.test.tsx`, and the four operations
sections through `pages/operations/OperationsPage.test.tsx`, as they are now.

## Amendments found while implementing (2026-10-17)

- **TracksPage hides its notice while empty.** The survey listed
  `pages/tracks/TracksPage.tsx` as following the rule. Its stylesheet has
  `.tracks-page__notice:empty { display: none; }`, the same breach as
  ArchiveTrack's. The rule is removed. Its focus move is unchanged.
- **BackupsSection computed its role.** Its finished-run paragraph was
  `role={failed ? "alert" : "status"}` and mounted with its line, so a
  success was a status region inserted with its text. The survey's grep
  for `role="status"` could not see it; the local fsd-reviewer on PR #401
  did. The alert and the status are now two elements, and the review
  criterion greps for a computed role too.
- **LineListEditor renders its region for the feeds kind only.** The
  editor serves other line-list kinds, which never check their lines and
  so never have a message. The region's condition is the kind, fixed for
  the editor's life, not the message.

## Proof / origin

- The fsd-reviewer finding on PR #387 (spec 149's post-merge review), which
  named BriefPanel and DescriptionPanel.
- The components that already follow the rule, listed above, are the
  pattern this spec extends.
- WAI-ARIA 1.2, `aria-live` and the `status` role; MDN, "ARIA live
  regions".

## Out of scope

- `role="alert"` regions. An alert is assertive and is announced on
  insertion by design; mounting it with its message is the documented use.
- Regions with no role that show text (`run-panel__last-line`, the Apply
  page's `run N: state` line). Whether they should be live is a separate
  question.
- A lint rule or test that finds a conditional `role="status"` across the
  app. It would read source text, which the review rules treat as a last
  resort. A custom ESLint rule is the better tool and is its own change.
- Changing any message's wording, timing or placement.
- Testing with a real screen reader. See limitations.

## Honest limitations

- The tests run in jsdom. They prove the DOM structure: the region exists
  first and the same element receives the text. They do not prove that any
  screen reader speaks it. No reader was run for this spec.
- Nothing stops a new component from mounting a region with its message.
  Review is the only guard until the lint rule above exists.
- Removing ArchiveTrack's `:empty` rule and adding empty regions to flex
  containers may add small gaps. The spec accepts this rather than hiding a
  region.

## Migration

None. No route, contract, stored data or command changes.
