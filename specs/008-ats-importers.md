---
spec: 008
title: ATS importers: Greenhouse, Ashby, Lever, RemoteOK
status: shipped
approved: yes
milestone: M2
depends: [007]
---

# Spec 008: ATS importers: Greenhouse, Ashby, Lever, RemoteOK

Refined from the stub before implementation; scope below is the real scope.

## Problem

The source modules are ingestion-only. Each produces the shared normalized shape
and nothing else; the runner glue that screens and persists arrives with the
orchestrator (spec 011).

## Scope

Package `harrier.sources`:

- `greenhouse.py`: board-token extraction, boards-api fetch with
  content=true, normalization (HTML-stripped content capped at 4000 chars).
- `ashby.py`: posting-api fetch with the HTML `window.__appData` fallback
  when the API fails (a hard-won path, pinned by fixture test), the
  tolerant job-node walker, location/description/compensation coalescing.
- `lever.py`: company-token extraction, EU API base selection for
  jobs.eu.lever.co boards, pagination at 100 per page.
- `remoteok.py`: public feed fetch, metadata/legal first-element skip,
  remote_signal="remote_only_board", url+title presence filter.
- `feeds.py`: one-URL-per-line config reading and netloc routing of
  config/feeds.txt to greenhouse/ashby/lever.
- `fetch_many` helper: per-board error isolation (one failing board never
  stops the others; errors are collected for the run summary). The old
  per-importer run_import loops carried this behavior; the pin ports here.

Boundary enforcement: a new import-linter contract "sources are ingestion
only" forbids `harrier.sources` from importing screening policy
(rules, pipeline, archetypes, descriptions, seen) and the tracker. Sources
may import `harrier.screening.normalized` (the shared shape) and
`harrier.screening.http` (fetch and strip primitives). The contract
enforces the module boundary; inline scoring logic written inside a source
module is still possible and stays a review and test concern
(data-integrity-reviewer audits it).

Deliberate changes from the old code, stated:

- No run_import / CLI / summary-file glue here; spec 011 owns orchestration.
- Logging module instead of the old log() file writes.
- The workable fixture is not ported: Workable support lived only in the
  dropped legacy orchestrator (parity matrix: drop).

## Acceptance criteria

- [ ] Normalization pins from the old tests/test_feed_importers.py pass:
      greenhouse token/company/external_id, ashby API shape incl.
      compensation string, ashby HTML fallback on API 404, lever
      normalization, lever EU API base selection
- [ ] fetch_many isolates a failing board and reports its error
- [ ] The import-linter contract fails on a sources module importing
      screening policy or the tracker
- [ ] All gates green on PR

## Proof / origin

Old repo: scripts/import_greenhouse_jobs.py, import_ashby_jobs.py,
import_lever_jobs.py, import_remoteok_jobs.py, run-job-imports.py
(parse_ats_feeds), tests/test_feed_importers.py and its synthetic fixtures
(copied; exampleco data only).

## Out of scope

Wellfound/WTTJ batch importers and capture endpoints (spec 010), Apify
(spec 009), orchestration, summaries, notify (spec 011).
