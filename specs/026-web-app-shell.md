---
spec: 026
title: Web app shell and styling
status: shipped
approved: yes
milestone: M6
depends: [005, 006, 021]
---

# Spec 026: Web app shell and styling

## Problem

The web app has no stylesheet at all. It renders as raw unstyled HTML:
default serif, no layout, a run log that fills the viewport and pushes the
tracker below the fold, and link-blue table rows. The README invites a
stranger to clone the repo and watch the system work, and this is what
they see.

It also undercuts two things already shipped. The demo (spec 021) is the
project's shop window. ADR-009 promised configuration that is easy to
customize, and spec 023 delivered a CLI and an API with no surface to use
them from.

## Scope

- An app shell: header, main column, sensible max width, spacing scale,
  light and dark via `prefers-color-scheme`.
- A real stylesheet, hand-written CSS with custom properties. No framework:
  the app has three surfaces and a dependency would outweigh them.
- Tracker table: readable density, aligned numerals for score, status as a
  legible badge rather than raw text, the URL on the title only.
- Run panel: the log collapsed by default with the last line and state
  visible, expandable. Today it is thousands of lines above the content
  anyone actually wants.
- Empty and error states for both surfaces.
- Accessible: focus states, contrast, the status filter labelled.

## Inputs, outputs, failure modes

The API contract, the routes and the data path are unchanged; every input
below is presentational or already exists in the components.

- Inputs: viewport width; `prefers-color-scheme`; the status filter
  (already in TrackerPage); the run controls (already in RunPanel); and one
  new control, the run log's expand/collapse, defaulting to collapsed and
  remembered for the session only.
- Outputs: the same data, laid out.
- Failure modes, all of which exist today and render as nothing:
  - jobs request fails: a stated error with a retry control
  - jobs request returns zero rows: a stated empty message that
    distinguishes "no jobs yet" from "no jobs match this filter"
  - jobs request in flight: a loading state that does not shift layout when
    it resolves
  - run stream disconnects: the panel says so rather than appearing to hang
  - a run ends in `failed` or `cancelled`: visible without expanding the log

## The run panel disclosure contract

Named because the panel is the thing most in need of it:

- Collapsed by default, including on first paint, with the run id, its
  state, and the most recent log line visible.
- No log yet: the line reads as waiting rather than rendering an empty box.
- Lines arriving over SSE while collapsed update the visible last line and
  do not expand the panel or steal scroll.
- The control is a real `button` with `aria-expanded`, operable by keyboard,
  keeping focus on itself when toggled.
- A run in `failed` opens the log automatically, because that is the only
  case where the log is the point. Its state and last line are visible
  either way.

## Acceptance criteria

Each names its proof. Automated ones are vitest symbols in
apps/web/src; browser checks are verified at 1280x800 and 720x800, light and
dark, and are recorded in the PR rather than by a test.

- [x] with a run holding thousands of log lines, the first tracker row stays
      within the first 900 vertical pixels (browser check)
- [x] the run panel renders collapsed on first paint, showing run id, state
      and last line, and expands and collapses by click and by keyboard
      (RunPanel.test.tsx::"the log is collapsed on first paint and the
      toggle reports its state")
- [x] the toggle is a button carrying `aria-expanded` that matches its state
      (same test)
- [x] lines arriving while collapsed update the last line and leave the
      panel collapsed (RunPanel.test.tsx::"lines arriving while collapsed
      update the visible last line without expanding")
- [x] a `failed` run opens its log without being asked
      (RunPanel.test.tsx::"a failed run opens the log without being asked")
- [x] a started run with no lines yet says it is waiting
      (RunPanel.test.tsx::"a started run with no lines yet says it is
      waiting")
- [x] a dropped log stream is stated rather than looking stuck
      (RunPanel.test.tsx::"a dropped stream says so instead of looking
      stuck")
- [x] zero rows renders a message, and the no-jobs and no-matches cases
      differ (JobTable.test.tsx::"empty list renders the message the page
      supplied", and TrackerPage supplies which one)
- [x] a failed jobs request renders a stated error and a retry control
      (browser check; the error path was exercised live during this work)
- [x] open rows outrank closed ones, and score orders within each group
      (JobTable.test.tsx::"open rows outrank closed ones however they
      scored", ::"rows are ordered by score, highest first")
- [x] a blank score reads as unknown rather than zero
      (JobTable.test.tsx::"a blank score renders as unknown rather than
      zero")
- [x] status carries a text label rather than colour alone
      (JobTable.test.tsx::"status carries a text label, not colour alone")
- [x] score renders with tabular numerals and right alignment (browser check)
- [x] body text meets 4.5:1 against its background in both schemes, and
      every interactive element has a visible focus style (browser check)
- [x] no horizontal scroll at 720px width with the longest real-shaped
      location string (browser check)
- [x] the generated contract is unchanged and existing web tests pass
- [x] All gates green: `just check`, run by the CI workflow. History: PR #25.it against a populated tracker

Two decisions read well against a handful of sample rows and failed against
a tracker that has been accumulating for months. Neither observation is
recorded here in numbers: a row count and its status distribution describe
someone's job search (ADR-008).

- Sorting on score alone fills the first screen with closed rows once most
  of the tracker is rejected, which is the steady state of any real search.
  Rows rank open before closed, then by score
  (JobTable.test.tsx::"open rows outrank closed ones however they scored").
- Job titles have no length limit and some carry a whole advert. Left to
  wrap they set the row height and take the density with them. Title and
  next action clamp to two lines with the full text in a tooltip.

One defect surfaced that is not presentational and is fixed under spec 005:
the header's health request made the app fetch twice concurrently, and
`/jobs` returned 500 with `sqlite3.ProgrammingError`. FastAPI runs a sync
dependency and its endpoint on different threadpool threads, so the
per-request connection was created in one and used in the other. Proven by
services/api/tests/test_api_jobs.py::
test_concurrent_requests_do_not_trip_the_sqlite_thread_check.

## Proof / origin

**Before this spec**, in the repository as it stood:

- No stylesheet exists: `find apps/web/src -name '*.css'` returns nothing,
  and no component imports one.
- The shell is three elements: apps/web/src/app/App.tsx renders `main`, an
  `h1` and the page, with no layout or tokens.
- The run log is unbounded and above the tracker:
  apps/web/src/features/runs/RunPanel.tsx renders every `lines` entry into
  a `pre`, with no disclosure control, and
  apps/web/src/pages/tracker/TrackerPage.tsx renders it before the table.
- The table is unstyled markup: apps/web/src/entities/job/JobTable.tsx
  emits a bare `table` and renders `status` and `score` as raw text.

**After this spec**, the proving files are
apps/web/src/shared/ui/tokens.css (the token layer, light and dark),
apps/web/src/app/App.tsx with widgets/header/Header.tsx (the shell),
apps/web/src/entities/job/ui/StatusPill.tsx and ScoreBar.tsx, and
apps/web/src/features/runs/RunPanel.tsx (the disclosure contract). Behaviour
is pinned by apps/web/src/features/runs/RunPanel.test.tsx and
apps/web/src/entities/job/JobTable.test.tsx, 13 web tests in total.

Honest limitation, unchanged by this spec: those tests assert content and
behaviour, never appearance. Nothing here can fail because the page looks
wrong, which is why the visual criteria are browser checks with viewports
and numbers rather than assertions.

Second limitation, and a real one: ScoreBar hardcodes the 0-120 scale and
the 55 cutoff. Scoring weights are user configuration (config/candidate.json,
spec 023), so a user who edits them moves the real cutoff and the tick
silently stops matching. Exposing the scale and cutoff through the contract
is the correct fix and is new API surface, so it belongs to its own spec
rather than being smuggled in here.

## Out of scope

A configuration editing surface (its own spec, since it needs new
interaction design rather than styling). Any change to the API contract.
A component library or CSS framework.
