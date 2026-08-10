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
is no longer retried (`is_retryable` in
services/api/src/harrier/screening/http.py, proven by
services/api/tests/test_screening.py::test_a_404_is_not_retried and
::test_a_403_is_not_retried, with ::test_a_503_is_still_retried,
::test_an_unlisted_5xx_is_still_retried, ::test_a_429_is_still_retried and
::test_a_timeout_is_still_retried pinning the other side). What is missing is the
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
  (services/api/src/harrier/discovery.py, the ATS loop's `extra=`, covered
  by
  services/api/tests/test_discovery.py::test_board_errors_are_recorded_per_source);
  this is the deliberate check, not a change to discovery.

- Bounded concurrency and a per-board budget, so one hung host cannot stall
  the report: at most 8 boards in flight, one logical probe each (this is a
  probe, not a fetch) with no retry, and a 15 second budget covering the
  whole probe including any redirects. At most 5 redirects are followed;
  the sixth reports the board unreachable rather than dead, since a
  redirect loop says nothing about whether the board exists.

## Inputs, outputs, failure modes

- Inputs: the configured feeds (store, then file, per spec 023).
- Outputs: a table of board, source, verdict, status; optionally a pruned
  configuration.
- The complete status-to-verdict contract, so nothing is classified by
  accident:

  The reported `status` is the final HTTP status where there is one. A
  failure with no HTTP response reports a stable token instead, one of
  `timeout`, `dns`, `connection`, or `invalid-body`, so the output is
  assertable rather than implementation-defined.

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

Proving symbols are named at implementation, in
services/api/tests/test_feed_health.py. They are deliberately not listed
here: this spec is not built yet, and naming symbols that do not exist is
how a spec starts lying (a mistake made and caught on PR #19).


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

A board watchlist is user configuration (ADR-009), and both its entries and
the shape of what it contains describe someone's job search (ADR-008), so no
observation of a real one is recorded here. The behaviour this spec addresses
is general: providers return 404 for boards that have closed, moved, or been
renamed, the entry stays in the configuration, and nothing surfaces it.
`harrier config check-feeds` is the tool for producing that list locally,
which is the point of the spec.

## Out of scope

Automatic pruning on a schedule. Greenhouse and Lever board discovery.
Suggesting replacements for a renamed board.
