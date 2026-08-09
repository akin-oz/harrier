---
spec: 010
title: Batch imports, capture endpoints, bookmarklets
status: shipped
approved: yes
milestone: M2
depends: [007, 008]
---

# Spec 010: Batch imports, capture endpoints, bookmarklets

Refined from the stub before implementation; scope below is the real scope.

## Problem

Two manual-export sources (Wellfound, WTTJ) and the browser capture path:
one click on a job page adds it to the tracker through the same scoring
pipeline as automated discovery.

## Scope

### harrier.sources.batch_exports (ingestion only, contract-bound)

- Shared CSV/JSON export reading (list payloads, items/results containers)
  and first-non-empty key picking.
- normalize_wellfound_row and normalize_wttj_row with the old key lists
  (wellfound: company/company_name/startup...; wttj incl. slug as an
  external-id fallback), board keys wellfound_export and wttj_export.

### harrier.capture (domain)

- add_captured_job: the manual-add path ported from the old
  jobs.py command_add. Validates company and title, truncates the
  description at 4000 chars, scores through the shared pipeline
  (score_job plus build_tracker_row, manual source label, manual_added
  note), inserts through the single write path (DuplicateJobError maps to
  a duplicate result), and caches the description on success. Manual adds
  are not subject to the score cutoff, exactly as before: a human clicked
  add, so it lands as a prospect regardless of score.

### harrier_api capture endpoints

- GET /capture/add with query params, returning the small HTML result
  page (the mixed-content dodge: a plain navigation from an HTTPS page to
  localhost is never blocked, unlike fetch). Same visual shape as the old
  page (icon, message, back-to-posting link).
- POST /capture/add with a JSON body for scripts and curl.
- Status contract preserved: 200 added, 400 missing company or title,
  409 already in tracker, 500 unexpected error.

### Bookmarklets

- docs/bookmarklets.md ported: the universal bookmarklet plus the four
  site variants (LinkedIn, Wellfound, WTTJ, HiringCafe) with their
  documented selector rationale, updated to port 8000 and /capture/add.
  The old job_server launchd section is dropped: the API service is the
  server now.

Deliberate changes, stated:

- The old job_server.py (second ad hoc HTTP server, importlib reload per
  request) is not ported; the endpoints live on the FastAPI service
  (parity matrix: change).
- Batch importers return normalized jobs only; run glue is spec 011.

## Acceptance criteria

- [ ] Wellfound and WTTJ normalization pins from the old
      tests/test_feed_importers.py pass
- [ ] CSV and JSON export reading round-trips, including the
      items/results container shape
- [ ] Capture pins from the old tests/test_job_server.py pass against the
      new endpoints: 200/400/409/500 for GET and POST, 4000-char
      description truncation, source defaults to manual
- [ ] A captured job goes through score_job and lands with the
      manual_added note; a duplicate returns 409
- [ ] All gates green on PR

## Proof / origin

Old repo: scripts/import_wellfound_export.py, import_wttj_export.py,
scripts/job_server.py, scripts/jobs.py command_add,
tests/test_job_server.py, docs/bookmarklets.md.

## Out of scope

Run orchestration and summaries (spec 011); the GUI capture settings page;
authentication (localhost only, per ADR-007 limitations).
