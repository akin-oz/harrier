---
spec: 151
title: A missing description is fetched from the posting
status: shipped
approved: yes
milestone: M10
depends: [007, 009, 047, 095, 106, 107, 149]
---

# Spec 151: A missing description is fetched from the posting

## Problem

Spec 149 made tailor, cover letter and answers refuse a job with no stored
description. That refusal is correct: before it, those jobs got a generic
CV or letter without a word. But on the hosted account many jobs have no
stored description, through no fault of the operator:

- **Rows found by hosted runs before 2026-10-16.** Until `45e69f2` ("Bind
  hosted runs to their owner's Storage prefix", spec 107, Amendment
  (2026-10-16): PR 3's build), a hosted run had no Storage binding.
  `files()` in `services/api/src/harrier/filestore.py` fell back to the run
  machine's own disk, so `cache_job_descriptions` wrote each description
  there and it was lost when the machine ended. On 2026-10-17 an Apify
  LinkedIn row added on the hosted account after the spec 141 import
  refused a cover letter, most likely for this reason.
- **Rows imported from the local store.** Spec 146's `import-artifacts`
  copies resumes, cover letters and answers only (`FAMILIES` in
  `services/api/src/harrier/artifactimport.py`). Job descriptions are out of
  its scope. Spec 152 covers those rows.

Spec 149's Migration section said jobs discovered before it "already have
their descriptions cached". That held for the local store and not for the
hosted account. This spec corrects it (Changes to approved specs).

The only remedy today is to paste each description by hand. The text is
usually still on the posting's page, and harrier already knows how to read
two kinds of page:

- LinkedIn's public guest endpoint, without login or Apify credit
  (`fetch_linkedin_job_details` in
  `services/api/src/harrier/screening/linkedin.py`, spec 009).
- The four ATS hosts on the enrichment allow-list (Greenhouse, Ashby,
  Lever, Workable), with every redirect checked against it
  (`enrich_url_allowed` and `enrich_job_description_for_scoring` in
  `services/api/src/harrier/screening/descriptions.py`, spec 007).

## Scope

1. One domain function fetches a job's description from its posting page,
   through the two existing readers, and stores it.
2. A Fetch button on the Apply page's description panel runs it for one
   job, with a CLI verb the route mirrors.
3. A backfill run fetches every missing description in the account, from
   the CLI and from the Operations page, locally and hosted.

## Behavior

### Which page a job's description is fetched from

`fetch_description(job_id, url)` in `services/api/src/harrier/apply/description.py`:

| The job's URL | Reader | Text kept |
|---|---|---|
| A LinkedIn job URL that `linkedin_job_id` reads an id from | The guest endpoint, as `fetch_linkedin_job_details` reads it | Its description markup, stripped, first 8,000 characters |
| A URL `enrich_url_allowed` accepts (Greenhouse, Ashby, Lever, Workable) | `http.request_text` with that allow-list on every redirect | The page stripped of HTML, first 8,000 characters |
| Any other URL | None | Refused before any request: `no description fetcher for <host>` |

The 8,000-character cut is the one both readers already make. The fetched
text is stored through `store_description` (spec 149), so it passes the
same checks as a pasted one.

A fetched text shorter than 120 characters after stripping is refused as
`the posting page held no description`, and nothing is stored. 120 is the
threshold the scoring enrichment already treats as no description
(`MIN_DESCRIPTION_LENGTH_FOR_SCORING`). A closed LinkedIn posting, a login
wall or an empty page all end here.

The function fetches only when nothing is stored. With a description
stored it refuses before any request: `a description is already stored for
job <id>`. Replacing a stored one stays the paste box's job (spec 149).

### One job: the Fetch button and `harrier description fetch`

- `POST /apply/{selector}/description/fetch`, no body, token required, runs
  `fetch_description` inside the request with a 30-second timeout per page
  and answers `DescriptionOut` (spec 149). It resolves the selector and
  track as the other description routes do. Its operation, `description
  fetch`, is allowed on every track kind (spec 094), as `description show`
  and `description set` are.
- `harrier description fetch JOB` runs the same function, prints
  `description fetched for job JOB`, and exits 1 with the refusal on any
  failure.
- On the Apply page, the panel's "no description" state (spec 149) gains a
  "Fetch from the posting" button beside the paste box. Fetching shows the
  stored text as a save does. A refusal shows in the API's words, and the
  paste box stays open with any text in it.

### Every job: the backfill

- `harrier description backfill [--dry-run] [--limit N]` walks every job
  in every track the account holds, in id order, and fetches for each job
  that is not `rejected`, has a URL, has no stored description, and has a
  reader. Up to `N` fetches with `--limit`. A dry run requests nothing and
  stores nothing, and reports what it would fetch.
- Fetches run one at a time, with a 2-second pause between requests to the
  same host, so the run never bursts at LinkedIn's guest endpoint.
- It prints one line per fetch attempt, `job <id>: fetched` or `job <id>:
  <refusal>`, and a closing summary of counts: checked, fetched, already
  stored, no URL, no fetcher for the host, page held no description, fetch
  failed, and skipped as rejected. A line names the job by id only, never
  the company, title or URL.
- It exits 0 when it walked every job, whatever the failures, because a
  posting that has closed is an expected outcome rather than a fault. It
  exits 1 only when it could not walk the jobs at all (the store did not
  open, or Storage refused on hosted).
- A run kind, `description-backfill`, with `--dry-run` and `--limit` as its
  parameters, takes no job and runs one at a time, as `backfill-posters`
  does (`services/api/src/harrier_api/runs.py`). It is offered hosted
  (`HOSTED_RUN_KINDS`): it writes through the run's binding to the owner's
  Storage prefix, which the description routes read (spec 107).
- `POST /ops/descriptions/backfill` with `{dry_run, limit}` starts it and
  answers `RunOut`, token required.
- The Operations page gains a "Job descriptions" section with "Dry run" and
  "Fetch missing" buttons, the run's output in the shared run panel, as the
  history backfill section does
  (`apps/web/src/pages/operations/HistorySection.tsx`).

## Failure modes

| Case | Result |
|---|---|
| A description is already stored | Refused before any request; the backfill counts it as already stored |
| The job has no URL | Refused: `job <id> has no URL; a description is stored by URL` (spec 149's words) |
| A host with no reader (RemoteOK, a company careers site) | Refused before any request: `no description fetcher for <host>` |
| A redirect to a host off the allow-list | The ATS reader refuses it, as enrichment does today; counted as fetch failed |
| The page answers 404, 429 or 5xx, or times out | Refused: `the posting page could not be read (<class>)`, naming the error class only; nothing stored |
| A closed posting, login wall or empty page | Refused: `the posting page held no description`; nothing stored |
| The stored write fails | As spec 149's write: the previous state stands, the route answers 500, the CLI exits 1 |
| Two fetches for one job at once (two tabs) | Both may fetch; the second store replaces the first with the same page's text. Harmless |
| A backfill while discovery runs | Discovery may store a description for a new row first; the backfill then counts it as already stored or stores the page's text over it. Either way a description is stored |
| Hosted run without a Storage binding | Refused as every hosted run is (spec 107): no local fallback, so nothing is written anywhere it cannot be read |
| Demo mode | A fetch outside the offline fixture set fails as fetch failed (`services/api/src/harrier/demo.py`); nothing is stored |

No log line or HTTP refusal carries the description, the company, the
title or the posting URL. The host appears only in the no-reader refusal,
which the operator asked for.

## Acceptance criteria

- [x] A LinkedIn job URL with no stored description is fetched from the
      guest endpoint, stored, and served by the read route (`services/api/tests/test_description_fetch.py::test_a_linkedin_description_is_fetched_and_stored`).
- [x] An allow-listed ATS URL is fetched and stored; a redirect off the
      list stores nothing (`services/api/tests/test_description_fetch.py::test_an_ats_description_is_fetched_and_a_bad_redirect_is_refused`).
- [x] An unsupported host, a stored description and a missing URL are each
      refused before any request (`services/api/tests/test_description_fetch.py::test_a_fetch_is_refused_before_any_request`).
- [x] A page with fewer than 120 characters of text, and a page that
      answers 429, store nothing and are refused in the words above
      (`services/api/tests/test_description_fetch.py::test_an_empty_or_failed_page_stores_nothing`).
- [x] `POST /apply/{selector}/description/fetch` and `harrier description
      fetch` reach the same function (`services/api/tests/test_description_fetch.py::test_the_fetch_route_and_verb_reach_one_function`).
- [x] The backfill fetches only non-rejected rows with a URL, no stored
      description and a reader, honours `--limit`, requests nothing on a
      dry run, and exits 0 when some fetches fail (`services/api/tests/test_description_fetch.py::test_the_backfill_fetches_only_what_is_missing`).
- [x] The backfill pauses between requests to one host (`services/api/tests/test_description_fetch.py::test_the_backfill_pauses_between_requests_to_one_host`).
- [x] `description-backfill` is offered hosted and reaches its start with
      the caller as owner (an added case of
      `services/api/tests/test_run_owner.py::test_a_kind_offered_hosted_reaches_its_start`).
- [x] The Apply panel's Fetch button shows the stored text on success and
      the API's words on a refusal (the tests "fetch from the posting stores and shows the description (spec 151)" and
      "a fetch refusal is shown in the API's words and the paste box stays" in
      `apps/web/src/features/description/DescriptionPanel.test.tsx`).
- [x] The Operations page starts a dry run and a fetch run (planned
      OperationsPage test).
- [x] No log line from a fetch or a backfill carries the URL, company,
      title or description (`services/api/tests/test_description_fetch.py::test_a_fetch_logs_no_posting_identity`).
- [x] The contract is regenerated and declares both routes and every
      refusal (`just contract`).

## Amendments found while implementing (2026-10-17)

- **One attempt per page.** The shared `request_text` logs each retry with
  the URL it retried (`services/api/src/harrier/screening/http.py`). A
  fetch therefore makes one attempt (`retries=1`), so no log line names the
  posting; the backfill's pause, not a retry, spaces the requests.
  `services/api/tests/test_description_fetch.py::test_a_linkedin_description_is_fetched_and_stored`
  pins the single attempt.
- **A LinkedIn URL is a linkedin.com URL.** `linkedin_job_id` reads
  `/jobs/view/<n>` on any host, so the id alone would send another site's
  job to LinkedIn's guest endpoint and store a different posting. The
  LinkedIn reader also requires a linkedin.com host or a subdomain of it
  (`is_linkedin_job_url` in `services/api/src/harrier/screening/linkedin.py`;
  the `example.com/jobs/view` case of
  `services/api/tests/test_description_fetch.py::test_a_fetch_is_refused_before_any_request`).

## Proof / origin

- The loss on hosted runs: spec 107, Amendment (2026-10-16): PR 3's build,
  and commit `45e69f2`.
- The readers: spec 009 (the guest endpoint) and spec 007 (the ATS
  enrichment and its allow-list, PR #4 review finding).
- The refusal that made the gap visible: spec 149.
- The run kind's shape: `backfill-posters`, spec 016.

## Out of scope

- Importing the local store's descriptions: spec 152.
- Replacing a stored description by fetching. Replace stays a paste.
- Readers for other hosts (RemoteOK, Workday, company careers pages). A
  new reader is its own change.
- Fetching through Apify. It costs money and the guest endpoint serves the
  same text.
- Running the backfill on a schedule, or fetching inside discovery.
  Discovery already stores what its sources return.
- Raising the 8,000-character cut or the ATS sources' 4,000-character cut.
- The job poster from a fetched page. `backfill-posters` covers posters.

## Honest limitations

- LinkedIn may throttle or block the guest endpoint, more readily from a
  datacenter address such as the hosted API's. The pause lowers the burst
  rate. It does not guarantee an answer. A blocked fetch is counted as
  fetch failed, and pasting stays the fallback.
- An ATS page is stored as its whole stripped text, navigation included,
  as scoring enrichment already stores it. The text is longer than the
  posting, not wrong.
- A posting that has closed since discovery cannot be fetched. Its job
  keeps refusing until a description is pasted.
- The 2-second pause is a choice, not a measured limit.

## Changes to approved specs

- **Spec 149, Migration.** It said jobs discovered before spec 149
  "already have their descriptions cached". On the hosted account, rows
  found by hosted runs before `45e69f2` and rows imported by spec 141 have
  none. The correction, with this spec's remedy, is added to spec 149 in
  the implementation change.

## Migration

After this ships, run the backfill once on the hosted account: a dry run
from the Operations page first, then "Fetch missing". Paste the rest by
hand. Locally, `harrier description backfill --dry-run` shows what is
missing, and the local store is usually complete already.
