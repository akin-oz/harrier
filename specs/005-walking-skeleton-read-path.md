---
spec: 005
title: Walking skeleton: tracker read path end to end
status: in-progress
approved: yes
milestone: M1
depends: [004]
---

# Spec 005: Walking skeleton: tracker read path end to end

Refined from the stub before implementation; scope below is the real scope.

## Problem

Every risky seam must be crossed once before feature work: FastAPI route,
exported OpenAPI document, generated TypeScript types, GUI consuming only
those types. Until this lands, the contract-drift gate checks nothing.

## Scope

### API (services/api/src/harrier_api)

- FastAPI app factory. Read-only endpoints:
  `GET /health` (name, version, database path, job count) and
  `GET /jobs?status=&source=` returning the full job rows (all tracker
  columns plus promoted columns, id, timestamps) via `harrier.tracker`.
- `JobStatus` is a Literal type in the API layer; a unit test asserts it
  matches `harrier.tracker.STATUSES` exactly (single-definition rule kept
  honest across the typing boundary).
- Invalid status filter is a 422 from validation, not a 500.
- Demo mode (`HARRIER_DEMO=1`): the app boots against a throwaway database
  seeded from `fixtures/demo-jobs.json` (synthetic rows, public class) so a
  stranger sees data without any personal database present.

### Contract (packages/contract)

- `python -m harrier_api.export_openapi` writes `packages/contract/openapi.json`
  deterministically (sorted keys, stable field order, trailing newline).
- `openapi-typescript` generates `packages/contract/src/schema.d.ts` from it.
- Both files are committed and never hand-edited. `just contract` runs both
  steps; the CI contract-drift job runs `just contract` and fails on any diff.

### Web (apps/web)

- `shared/api`: an `openapi-fetch` client typed by the generated `paths`.
- `entities/job`: the Job type re-exported from the contract package plus a
  presentational JobTable.
- `pages/tracker`: TrackerPage fetching `/jobs` through the client with
  TanStack Query, with loading and error states, plus a status filter.
- `app`: QueryClientProvider wiring; App renders TrackerPage.
- All data access goes through the generated client; no hand-typed response
  shapes (fsd-reviewer and contract-guardian both audit this).

## Acceptance criteria

- [ ] `just contract` is deterministic: running it twice produces no diff
- [ ] A route change without regeneration fails the contract-drift CI job
- [ ] An invented field access in apps/web fails `tsc`
- [ ] API tests cover /jobs empty, seeded, filtered, and invalid-status 422
- [ ] The GUI lists the migrated real rows locally (manual verification) and
      fixture rows in demo mode
- [ ] All gates green on PR

## Proof / origin

docs/adr/ADR-005-api-contract-seam.md; docs/adr/ADR-001-frontend-framework.md;
harrier.tracker (spec 004).

## Out of scope

Mutations (tracker verbs arrive with their own specs), the run manager and SSE
(spec 006), pagination (701 rows renders fine; revisit when real), TanStack
Router (single page today; routing lands with the second page), auth (localhost
tool, ADR-007 limitations).
