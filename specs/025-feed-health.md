---
spec: 025
title: Feed health: report and prune dead boards
status: proposed
approved: no
milestone: M6
depends: [023]
---

# Spec 025: Feed health: report and prune dead boards

## Problem

17 of the 340 Ashby boards in the real watchlist answer 404. They are
companies that closed their board, moved off Ashby, or renamed it. Nothing
tells the operator which, so they accumulate: every run pays for them and
prints failures that make a working system look broken.

The retry defect behind the noise is already fixed (a non-transient status
is no longer retried). What is missing is the ability to find and remove
the dead entries rather than re-discovering them in a log every four hours.

## Scope

- `harrier config check-feeds`: probe every configured board across
  Greenhouse, Ashby, Lever, and report each as live, dead, or unreachable,
  with the status that decided it. Concurrent, read-only, no writes.
- `--prune`: remove the dead entries from the configuration store,
  reporting exactly what was removed. Never automatic: a board that 404s
  during an outage is not dead, so removal is a decision the operator makes
  after seeing the report.
- The run summary keeps recording `board_errors` as it does now; this is
  the deliberate check, not a change to discovery.

## Inputs, outputs, failure modes

- Inputs: the configured feeds (store, then file, per spec 023).
- Outputs: a table of board, source, verdict, status; optionally a pruned
  configuration.
- Failure modes: a network failure reports the board as unreachable rather
  than dead, and unreachable boards are never pruned.

## Acceptance criteria

- [ ] every configured board is probed and classified live, dead, or
      unreachable
- [ ] a transient failure classifies as unreachable, not dead
- [ ] --prune removes only dead entries and reports each one
- [ ] --prune without a prior clean probe of that board is refused
- [ ] All gates green on PR

## Proof / origin

Observed on the real watchlist: 340 Ashby boards, 323 live, 17 dead
(DoseSpot, Roam, beam, comulate, lindushealth, mobasi, mosey, olive,
optery, proofofplay, retool, statsig, and five more).

## Out of scope

Automatic pruning on a schedule. Greenhouse and Lever board discovery.
Suggesting replacements for a renamed board.
