---
spec: 150
title: Discovery stages the Apify job poster as a contact candidate
status: accepted
approved: yes
milestone: M6
depends: [009, 011, 016]
---

# Spec 150: Discovery stages the Apify job poster as a contact candidate

## Problem

The Apify LinkedIn actor (`curious_coder/linkedin-jobs-scraper`) returns the
person who posted a job when LinkedIn shows one: `jobPosterName`,
`jobPosterTitle`, `jobPosterProfileUrl` and `jobPosterPhoto`. A live
dataset checked on 2026-10-16 carried them on some items and omitted the
keys entirely on the rest. The profile URLs were `https` links on country
subdomains, such as `uk.linkedin.com/in/<slug>`.

Discovery drops them:

- `extract_publisher_contact` in
  `services/api/src/harrier/screening/linkedin.py` reads poster fields from
  a raw Apify item. Spec 009 ported it and left the staging write to
  spec 016. Nothing in `services/api/src/` calls it. Only
  `services/api/tests/test_apify_linkedin.py` does.
- Its URL key list does not include `jobPosterProfileUrl`, the key this
  actor uses. Called on a live item, it would return the name and title and
  drop the profile link.

So the operator finds the poster again by hand, or runs `backfill-posters`
(spec 016, `services/api/src/harrier/outreach/backfill.py`). That fetches
LinkedIn's guest page per job to get a person the paid run already
returned.

## Scope

1. The discovery run path stages the poster from each added Apify
   LinkedIn job as a pending contact candidate.
2. `extract_publisher_contact` reads this actor's profile URL key.
3. A staged profile URL is checked and written in one form.

## Behavior

### When a poster is staged

During a discovery run that is not a dry run, for each Apify LinkedIn job
the run adds to the tracker:

1. The poster is read from that job's raw Apify item with
   `extract_publisher_contact`. `jobPosterProfileUrl` joins its URL keys.
   `jobPosterName` and `jobPosterTitle` are already in its name and title
   keys.
2. With neither a name nor a usable profile URL, nothing is staged.
3. Otherwise the poster is staged as a pending candidate in the job's
   candidates artifact, through the same merge `backfill-posters` uses
   (`_stage_poster`), moved to a module both callers import.

The staged candidate is the one `backfill-posters` writes, with two fields
that say where it came from: `source` is `apify_linkedin` and `raw_query`
is `apify_discovery`. `review_status` is `pending`. It appears on the
outreach page's candidates list and is approved or rejected there
(spec 016). Nothing writes the contacts table.

The staging runs in the run path (`services/api/src/harrier/discovery.py`),
after the row is added. The source module stays ingestion only.

### The profile URL

A profile URL is usable when it parses, its scheme is `https`, its host is
`linkedin.com` or a subdomain of it, and its path starts with `/in/`. A
usable URL is staged in one form: `https://www.linkedin.com/in/<slug>`,
with no query, fragment or trailing slash. So a poster on `uk.linkedin.com`
and the same person on `www.linkedin.com` compare equal. An unusable URL is
dropped, and the poster is staged by name alone if there is one.

`jobPosterPhoto` is not stored.

### Duplicates

A poster is not staged when the job's candidates artifact already holds a
candidate with the same URL, or with the same name when neither has a URL.
It is not staged when the contacts table already holds that profile URL.
Both checks are the ones `backfill-posters` already makes.

### What the run reports

The run logs one line per run: `apify posters staged: <n>`. The line
carries no name, title or URL. The per-source summary does not change.

## Failure modes

| Case | Result |
|---|---|
| Dry run | Nothing is staged. |
| The job was a duplicate, rejected, held or already seen | Nothing is staged. Only rows this run adds get a poster. `backfill-posters` covers rows already in the tracker. |
| The tracker add raised a duplicate (a concurrent add) | Nothing is staged for that job. |
| The item has poster keys with empty values | Treated as absent. |
| The name is present and the URL is a company page, or not `https` | Staged by name, with no URL. |
| Reading or writing the candidates artifact fails | Logged by error class only. That job's poster is not staged. The run continues and the job stays added. |
| The same poster posted several jobs in one run | Staged once per job, because candidates are kept per job. Approval links the one contact to each job (spec 036). |
| A dataset-file replay (`--dataset-file`) | Stages as a live run does. It is not a dry run. |
| A focused-track placement (spec 139) | Staged against the job on the track it was placed in. |
| Hosted run | The artifact goes through `files()`, the owner's Storage (spec 107). |

## Acceptance criteria

- [ ] An Apify item with `jobPosterName`, `jobPosterTitle` and
      `jobPosterProfileUrl` on a `uk.linkedin.com/in/` link, run through
      discovery and added to the tracker, leaves one pending candidate with
      that name and title and `https://www.linkedin.com/in/<slug>`
      (planned test_discovery_stages_the_apify_poster).
- [ ] The same run as a dry run stages nothing
      (planned test_a_dry_run_stages_no_poster).
- [ ] An item that screening rejects, or that is a tracker duplicate,
      stages nothing (planned test_only_added_rows_stage_a_poster).
- [ ] An item with no poster keys stages nothing, and an item whose URL is
      not an `https` LinkedIn `/in/` link stages the name with no URL
      (planned test_an_unusable_poster_url_is_dropped).
- [ ] A poster already staged for the job, or already in the contacts
      table, is not staged again
      (planned test_a_known_poster_is_not_staged_twice).
- [ ] A candidates artifact write that raises leaves the job added and the
      run finished (planned test_a_staging_failure_does_not_fail_the_run).
- [ ] No contact row is written by discovery (the same tests read the
      contacts table).
- [ ] `extract_publisher_contact` reads `jobPosterProfileUrl`
      (an added case in `test_extract_publisher_contact_flat_and_nested`).
- [ ] `backfill-posters` behaves as before
      (the existing tests in `services/api/tests/test_outreach.py`).

## Proof / origin

- The actor's fields: a live dataset from
  `curious_coder/linkedin-jobs-scraper`, read on 2026-10-16. Only key names
  and URL shapes were recorded. No poster data is in this repository.
- The unwired extractor and the deferred write: spec 009, "Amendments".
- Staging, not writing contacts: the "Nothing auto-sends" invariant and
  spec 016.

## Out of scope

- The job description. Spec 149.
- Rows already in the tracker. `backfill-posters` stages theirs.
- Apify sources other than LinkedIn, and the guest-page poster path.
- Storing the poster's photo, or the company's LinkedIn URL, logo or size.
- Canonicalizing LinkedIn URLs in the contacts table or anywhere other
  than this staging.
- Approving a poster automatically.

## Migration

None. The next discovery run stages posters for the rows it adds. To stage
posters for rows added before this change, run `backfill-posters` as
today.

## Least confident

- The country-subdomain rewrite assumes `xx.linkedin.com/in/<slug>` and
  `www.linkedin.com/in/<slug>` name the same profile. That is LinkedIn's
  observed behavior, not a documented guarantee.
- Staging only for added rows. Staging for tracker duplicates too would
  catch a poster that appears on a later run, at the cost of a second
  write path into older rows' artifacts.
