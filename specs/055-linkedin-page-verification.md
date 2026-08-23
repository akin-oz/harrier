---
spec: 055
title: LinkedIn prospects are verified against the posting's own page
status: accepted
approved: yes
milestone: M8
depends: [009, 053]
---

# Spec 055: LinkedIn prospects are verified against the posting's own page

## Problem

Discovery still surfaces hybrid and on-site LinkedIn postings after spec
053. The gate 053 built is only as good as its inputs, and both inputs
fail in practice:

1. The actor does not deliver the declaration. Spec 053 reads
   `workplaceTypes` from the Apify item, but every LinkedIn prospect
   currently in the tracker arrived with a bare-city location and no
   declaration prefix, so the field is absent from the datasets the
   default actor actually returns.
2. Text evidence over-matches. With no declaration, the gate accepts on
   `REMOTE_POSITIVE_PATTERNS` anywhere in the posting text, and hybrid
   postings routinely mention remote incidentally: "hybrid working model,
   2 remote days per week", "20 Flex Days per year to work remotely".
   The negative hints stay deliberately scoped to the location field
   (spec 032), so these pass.

Ground truth exists and is free: the posting's own public page. LinkedIn
embeds a schema.org `JobPosting` JSON-LD block in the logged-out job view
page and sets `jobLocationType: "TELECOMMUTE"` exactly when the poster
tagged the job remote; a posting tagged hybrid or on-site carries the
JobPosting block without the field. Measured on this machine across the
LinkedIn prospects then in the tracker (2026-08-23): a single page
declared TELECOMMUTE, the large majority carried JobPosting JSON-LD
without it (and a third of those openly admit hybrid or
office-attendance language in their cached descriptions), and the rest,
roughly a quarter, exposed no JSON-LD at all (stable across a retry
against the canonical `www.linkedin.com` host; not an authwall).

## Scope

- `services/api/src/harrier/screening/linkedin.py`: a page-verdict helper
  (fetch the job view page, parse JSON-LD, return declared-remote,
  declared-not-remote, or unknown) with a per-URL verdict cache.
- `services/api/src/harrier/screening/pipeline.py`: a verification step
  for jobs whose `remote_signal` is `linkedin_search` that survived the
  existing gates, with its own rejection slug.
- `services/api/src/harrier/discovery.py`: one field in the per-source
  run summary carrying the unverified count.
- Tests in `services/api/tests/test_screening_linkedin_page.py` (new) and
  pipeline tests.

No source-module change, no rules.py change, no contract change. The
existing gates stay exactly as spec 053 left them; this runs after them.

## Behavior

For each `apify_linkedin` job that passes `title_allowed` and
`remote_region_allowed`, the shared screening path verifies the posting
against its own page before the tracker append:

- The page is `https://www.linkedin.com/jobs/view/{job_id}`, with the id
  taken by the existing `linkedin_job_id`. The fetch goes through
  `screening.http.request_text` with the existing retry policy, and the
  URL is constructed, never taken from the posting, so no crafted URL can
  redirect the check.
- The verdict comes from schema.org JSON-LD: any
  `<script type="application/ld+json">` block whose `@type` is
  `JobPosting` (top-level object or list member).
  - `jobLocationType` equal to `TELECOMMUTE` (case-insensitive):
    **declared remote**. The job proceeds.
  - JobPosting present, `jobLocationType` absent or any other value:
    **declared not remote**. The job is rejected with the reason slug
    `linkedin_page`, counted in `rejected_counts` like the other gates,
    and recorded in the seen store so it is not re-fetched next run.
  - No JobPosting block, or the fetch fails after retries: **unknown**.
    The prior gates' acceptance stands (fail-open), and the run summary
    counts the job as unverified. LinkedIn withholding structured data is
    a fact about the page shape, not about the job; failing closed would
    turn a page-format change into empty discovery.
- Verdicts are cached per job URL under the data directory
  (never-in-git), alongside the description cache, so each posting is
  fetched at most once across runs. `unknown` is not cached: a later run
  may see a page shape that answers.
- A job with no extractable LinkedIn job id is treated as unknown.
- Only `linkedin_search` jobs are verified. The other sources' postings
  have no LinkedIn page to consult and their existing gates already read
  the provider's own workplace field.

## Failure modes

- **LinkedIn blocks or throttles the fetch**: retries per the existing
  http policy, then unknown, fail-open, counted. A block cannot empty a
  discovery run.
- **Page shape changes and JSON-LD disappears everywhere**: every job
  becomes unknown and screening degrades exactly to spec 053 behavior,
  visibly (the unverified count covers the whole run), never silently to
  something new.
- **A posting is taken down between the actor run and verification**: the
  fetch typically 404s or serves a page without JobPosting; unknown,
  fail-open. A dead posting that reaches the tracker dies in triage like
  any other.
- **Malformed JSON in a matching script block**: that block is skipped;
  other blocks may still answer; otherwise unknown. Never a crash.
- **The verdict cache is deleted**: postings are re-fetched once and the
  cache rebuilds. Nothing else depends on it.

## Acceptance criteria

- A page whose JobPosting JSON-LD has `jobLocationType: "TELECOMMUTE"`
  yields declared-remote and the job is appended.
- A page whose JobPosting JSON-LD lacks `jobLocationType` yields
  declared-not-remote and the job is rejected with reason slug
  `linkedin_page`, visible in `rejected_counts`.
- A page with no JSON-LD, and a fetch that raises, both yield unknown and
  the job is appended; the run result carries the unverified count.
- A second screening of the same URL performs no second fetch when the
  first verdict was declared-remote or declared-not-remote (proved with a
  counting fake fetcher).
- A non-LinkedIn job triggers no fetch at all.
- The fetched URL is always on `www.linkedin.com`, regardless of the
  posting URL's host.
- Tests fake the fetcher; no test performs a network call.

## Proof / origin

The leak is observable in this machine's tracker: prospects accepted
after spec 053 whose cached descriptions say "hybrid working model" or
"On-site primarily". The page signal was measured directly on 2026-08-23
over every LinkedIn prospect then in the tracker (proportions in the
Problem section) with the known-hybrid rejects as negative controls:
every one of them carries
JobPosting JSON-LD without `jobLocationType`, and the one posting
LinkedIn tags remote carries `TELECOMMUTE`. The guest fragment endpoint
(`jobs-guest/jobs/api/jobPosting/{id}`, spec 009) was checked first and
does not carry the workplace type; the full job view page does. The
fetch, retry, and HTML plumbing this reuses is
`services/api/src/harrier/screening/http.py` (spec 007) and
`services/api/src/harrier/screening/linkedin.py` (spec 009).

## Out of scope

- Re-screening rows already in the tracker. Done as a one-off operator
  pass on 2026-08-23, rejecting the prospects whose pages disclaimed the
  remote tag; the spec covers new arrivals.
- Description-text hybrid detection. The location-scoped negative hints
  (spec 032) stay untouched.
- Any change to the Apify actor input or to spec 053's gates.
- Fetching pages for the other job sources.
- A fail-closed mode for unknown verdicts. If the unverified count shows
  LinkedIn withholding data at scale, that is a new decision for a new
  spec.

## Limitations

- The check trusts LinkedIn's own remote tag. A poster who tags remote
  and requires office days still passes; a genuinely remote job whose
  poster tagged a city is rejected. The tag is the posting's public
  claim, and enforcing it is the strongest guarantee available without a
  human reading every posting.
- Unknown pages (roughly a quarter in the measurement) pass on spec
  053's weaker text evidence. The unverified count makes the size of that
  hole visible per run.

## Migration

None. First run after the change fetches one page per surviving LinkedIn
job and builds the verdict cache.
