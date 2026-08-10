---
spec: 025
title: Feed health: report and prune dead boards
status: accepted
approved: yes
milestone: M6
depends: [023]
---

# Spec 025: Feed health: report and prune dead boards

## Problem

A watchlist accumulates dead boards. Companies close their board, move off
a provider, or rename it, and the entry stays in the configuration
answering 404 forever. Nothing tells the operator which entries those are,
so every run pays for them and prints failures that make a working system
look broken.

The retry defect behind the noise is already fixed: a non-transient status
is no longer retried (harrier/screening/http.py `is_retryable`, proven by
tests/test_screening.py::test_a_404_is_not_retried). What is missing is the
ability to find and remove dead entries rather than re-discovering them in
a log every four hours.

## Scope

- `harrier config check-feeds`: probe every configured board across
  Greenhouse, Ashby, Lever, and report each as live, dead, or unreachable,
  with the status that decided it. Concurrent, read-only, no writes.
- `--prune`: remove the dead entries from the configuration store,
  reporting exactly what was removed. Never automatic: a board that 404s
  during an outage is not dead, so removal is a decision the operator makes
  after seeing the report.
- The run summary keeps recording `board_errors` as it does now
  (harrier/discovery.py, the ATS loop's `extra=`); this is the deliberate
  check, not a change to discovery.

- Bounded concurrency and a per-board budget, so one hung host cannot stall
  the report: at most 8 in flight, one attempt per board (this is a probe,
  not a fetch), a 15 second timeout, and no retry. A board that times out
  is unreachable, never dead.

## Inputs, outputs, failure modes

- Inputs: the configured feeds (store, then file, per spec 023).
- Outputs: a table of board, source, verdict, status; optionally a pruned
  configuration.
- The complete status-to-verdict contract, so nothing is classified by
  accident:

  | Observation | Verdict |
  |---|---|
  | 2xx with a parseable body | live |
  | 3xx to a board on the same provider | live (followed) |
  | 404, 410 | dead |
  | 401, 403 | unreachable (it may exist behind auth) |
  | 408, 429, any 5xx | unreachable |
  | timeout, DNS or connection failure | unreachable |
  | 2xx whose body does not parse as a board | unreachable, not dead |

  Only `dead` is ever prunable. Everything else, including a malformed
  response, keeps the entry: an outage must never delete a watchlist.
- `--prune` acts only on a probe performed in the same invocation. There is
  no stored report to go stale, which removes the freshness question rather
  than answering it.

## Acceptance criteria

- [ ] every configured board is probed and classified by the table above,
      with one test per row
- [ ] a timeout, a 429, a 5xx, a 403 and an unparseable 200 all classify as
      unreachable, and none of them is prunable
- [ ] at most 8 probes run concurrently, and one hung host does not prevent
      the other boards being classified
- [ ] --prune removes only entries this invocation probed as dead, and
      reports each one
- [ ] --prune with no probe in the same invocation is refused
- [ ] the report names no board that is not already in the operator's own
      configuration, and nothing is written to a committed file
- [ ] All gates green on PR

## Proof / origin

The behavior class was observed on a real watchlist, where a minority of
Ashby entries answered 404 while the provider API itself was healthy. The
specifics are not recorded here: a board watchlist is user configuration
(ADR-009) and naming its entries in a public repository publishes part of
someone's job search (ADR-008). `harrier config check-feeds` is precisely
the tool for producing that list locally, which is the point of the spec.

## Out of scope

Automatic pruning on a schedule. Greenhouse and Lever board discovery.
Suggesting replacements for a renamed board.
