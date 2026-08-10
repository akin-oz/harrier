---
spec: 026
title: Web app shell and styling
status: accepted
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
- A run in `failed` shows its state and last line without expansion,
  because that is the only case where the log is the point.

## Acceptance criteria

Each is a vitest assertion unless it says browser check, in which case it is
verified at 1280x800 and at 720x800 in the preview browser, light and dark.

- [ ] with a run holding 2000 log lines, the first tracker row is within the
      first 900 vertical pixels (browser check at 1280x800)
- [ ] the run panel renders collapsed on first paint, showing run id, state
      and last line, and expands and collapses by click and by keyboard
- [ ] the toggle is a button carrying `aria-expanded` that matches its state
- [ ] lines arriving while collapsed update the last line and leave the
      panel collapsed
- [ ] a `failed` run shows its state and last line while collapsed
- [ ] zero rows renders a message, and the no-jobs and no-matches cases
      differ
- [ ] a failed jobs request renders a stated error and a retry control
- [ ] score renders with tabular numerals and right alignment, and status
      renders with a text label rather than colour alone
- [ ] body text meets 4.5:1 against its background in both schemes, and
      every interactive element has a visible focus style (browser check)
- [ ] no horizontal scroll at 720px width with the longest real-shaped
      location string (browser check)
- [ ] the generated contract is unchanged and existing web tests pass
- [ ] All gates green on PR

## Proof / origin

In the repository, which is where the claims are checkable:

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

Supporting evidence: the demo screenshot from 2026-08-10, showing serif
defaults with the tracker below the fold.

Limitations of the existing tests: App.test.tsx, JobTable.test.tsx and
RunPanel.test.tsx assert content and behavior only. None of them asserts
anything visual, so they will keep passing throughout this work and cannot
be cited as evidence for any of it. That is why the criteria above name
browser checks with viewports and numbers.

## Out of scope

A configuration editing surface (its own spec, since it needs new
interaction design rather than styling). Any change to the API contract.
A component library or CSS framework.
