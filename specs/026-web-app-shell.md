---
spec: 026
title: Web app shell and styling
status: proposed
approved: no
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

- Inputs: none new. This is presentation over the existing contract.
- Outputs: a styled app at the same routes.
- Failure modes: none new; no data path changes.

## Acceptance criteria

- [ ] the tracker is visible without scrolling past the run log
- [ ] light and dark both legible, verified in the browser
- [ ] the run panel collapses and expands, and shows state when collapsed
- [ ] empty tracker and failed fetch both render a stated message
- [ ] existing web tests still pass and the contract is unchanged
- [ ] All gates green on PR

## Proof / origin

The demo screenshot from 2026-08-10: unstyled serif, the run log occupying
the entire viewport, the tracker below the fold.

## Out of scope

A configuration editing surface (its own spec, since it needs new
interaction design rather than styling). Any change to the API contract.
A component library or CSS framework.
