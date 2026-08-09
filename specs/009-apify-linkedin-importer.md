---
spec: 009
title: Apify LinkedIn importer
status: in-progress
approved: yes
milestone: M2
depends: [007, 008]
---

# Spec 009: Apify LinkedIn importer

Refined from the stub before implementation; scope below is the real scope.

## Problem

The only paid source. Its cost-savers are the point: the 24h search window
lives in config, descriptions are cached for everything fetched (even jobs
that screening rejects), and the dataset-file mode replays a downloaded run
without re-billing.

## Scope

### harrier.sources.apify_linkedin (ingestion only, contract-bound)

- Env config: APIFY_TOKEN required for live runs, APIFY_LINKEDIN_ACTOR
  optional (default curious_coder/linkedin-jobs-scraper).
- Actor lifecycle: start run, poll to a terminal status with a timeout
  budget, fetch the dataset; the data-wrapper unwrap; an Apify-specific
  request helper (POST with JSON body, retries). Token stays in the query
  string per the Apify API; endpoint URLs are never logged.
- normalize_apify_job with the multi-shape field probing (Title/title,
  Detail URL/detailUrl/link/url, ...) and remote_signal="linkedin_search"
  (the screening region bypass for query-scoped searches).
- Dataset-file mode: load one or more downloaded dataset JSON files instead
  of a live run.
- Search URLs from config/linkedin_search_urls.txt (public; the f_TPR=r86400
  cost-saver comment carries over).
- DEFAULT_COUNT stays 150 (the count-value discrepancy across the old repo
  resolves in spec 011's scheduled-run policy, per the parity matrix).

### harrier.screening.linkedin (guest endpoint helpers)

Lives in screening, not sources, because it does description caching (the
sources contract forbids cache access) and its consumers span enrichment,
backfill, and outreach:

- linkedin_job_id: both URL shapes (/jobs/view/123 and the slugged variant,
  plus currentJobId query params).
- Guest JD fetch via the public jobPosting endpoint (no auth, no Apify
  credits), description extraction, cache integration
  (fetch_linkedin_jds short-circuits on cached URLs).
- Poster extraction: from guest HTML (message-the-recruiter section) and
  from raw Apify payloads (extract_publisher_contact with the defensive
  key probing across actor variants).
- cache_job_descriptions helper in screening.descriptions: the
  cache-everything-fetched behavior, called by the run path.

## Amendments (stated, not silent)

- The old link_publisher_contacts wrote contacts directly; the parity matrix
  routes this through the staged-contacts path instead. The staging store
  arrives with spec 016, so this spec ports the pure extraction functions
  and spec 016 wires the staging write. Nothing writes contacts here.
- The old module parsed .env itself; env loading becomes the CLI entry
  point's job in spec 011. This module reads os.environ only.
- The old run_import glue (screening, summary, notify) belongs to spec 011.

## Acceptance criteria

- [ ] Pins from the old tests/test_import_apify_linkedin_jobs.py pass:
      default count 150, actor input shape, wrapped-payload unwrap,
      field mapping, dataset-file loading (hybrid/non-EMEA/dedupe pins
      already live in the spec 007 suite)
- [ ] linkedin_job_id handles both URL shapes and currentJobId
- [ ] Poster extraction pins: flat keys, nested keys, guest HTML section,
      and the /in/ URL requirement
- [ ] cache_job_descriptions caches all jobs with url and description
- [ ] All gates green on PR

## Proof / origin

Old repo: scripts/import_apify_linkedin_jobs.py;
tests/test_import_apify_linkedin_jobs.py; config/linkedin_search_urls.txt
(copied; search keywords only); OPERATIONS.md "Apify LinkedIn Import".

## Out of scope

Run orchestration, summaries, notify, weekday-morning policy (spec 011);
staged contact writes and approval (spec 016); the profile-search actor for
outreach (spec 016).
