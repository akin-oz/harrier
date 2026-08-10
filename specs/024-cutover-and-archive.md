---
spec: 024
title: Cutover and archive
status: proposed
approved: no
milestone: M5
depends: [022]
---

# Spec 024: Cutover and archive

## Problem

Split from spec 022, which covered verification and the cutover event
together. Verification is read-only, repeatable, and shipped. This is the
other half: one irreversible sitting that stops the system running Akin's
job search and starts a different one in its place.

It is separated because the two need different gates. Verification needed a
code review. This needs a person deciding that parity is reached, on a day
they can watch the first scheduled run land.

## Scope

Cutover plan phases 3 and 4 (docs/cutover-plan.md), unchanged in substance:

- quiesce: unload the three old plists
- snapshot the old tracker, state, and gmail event log
- final migration refresh, including the discovery seen-state that spec 022
  named as a precondition for a meaningful screening diff
- verify: row counts, schedule status, one dry-run discovery and digest
- go live: harrier schedule install, watch the next scheduled run
- one-week fallback window, then archive the old repo read-only

## Preconditions, to be met before this is approved

- `harrier parity status` reports every checklist item checked or waived.
- At least one `harrier parity diff` over a post-migration shadow run is
  clean, including a weekday morning so the Apify path and its cost gate
  are exercised. Before the seen-state migration the diff declines to
  compare screening at all, so this cannot be satisfied early.
- The `.env` defect in the old repo is fixed or knowingly accepted: a value
  spanning a line break makes the shell wrapper exit 127 under `set -e`,
  which is why the old digest stopped running. It matters here because the
  fallback window depends on the old system still working.

## Acceptance criteria

- [ ] every parity row checked or explicitly waived by Akin
- [ ] one post-migration shadow diff clean over a full weekday cycle
      including an Apify morning
- [ ] old plists unloaded, new plists live, first scheduled run observed
- [ ] fallback window documented with its expiry date
- [ ] old repo tagged archived, plists removed, cutover date and final row
      counts recorded in docs/cutover-log.md

## Proof / origin

docs/cutover-plan.md phases 3 and 4.

## Out of scope

Anything spec 022 covers. Deleting the old repo: it stays on disk,
read-only, and its history stays private forever regardless of archival
(docs/privacy-plan.md).
