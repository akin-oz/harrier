---
spec: 149
title: The Apply page shows the stored description and takes one when none was captured
status: proposed
approved: no
milestone: M6
depends: [009, 013, 047, 079, 094, 095]
---

# Spec 149: The Apply page shows the stored description and takes one when none was captured

## Problem

Discovery already stores each posting's description. The run path caches
it by URL for every job that carries one
(`cache_job_descriptions` and `save_description_cache` in
`services/api/src/harrier/screening/descriptions.py`, called from
`services/api/src/harrier/discovery.py` and
`services/api/src/harrier/screening/pipeline.py`). The Apify LinkedIn actor
returns the full text in `descriptionText`, and `normalize_apify_job` reads
it. A recent live dataset carried it on every item.

The four application operations already read that cache when they are
given no text: `run_tailor` in `services/api/src/harrier/resume/tailor.py`,
and the `cover-letter`, `answers` and `evaluate` verbs in
`services/api/src/harrier_cli/main.py`.

What the operator cannot do is see any of this. So they paste the
description anyway, because nothing tells them it is already there.

- The Apply page (`apps/web/src/pages/apply/ApplyPage.tsx`) offers a
  "Job description (optional)" box on the resume and evaluate operations,
  with the placeholder "Leave empty to use the description captured at
  import." Nothing says whether one was captured.
- A pasted description is used for that one run and then dropped
  (`_apply_params` in `services/api/src/harrier_api/app.py` stages it as a
  run input file). The next operation does not see it.
- Cover letter and answers take no description from the browser at all.
  For a job with nothing cached, they run against an empty posting.
- With no text and no cache, tailoring runs anyway with
  `jd_source: "none"` in its sidecar and produces an untailored CV. Nothing
  on the page or in the CLI output says so.

Who it hurts: the operator, who pastes text the system already holds, and
who can send an untailored CV without knowing it.

## Scope

1. A read route and a write route for a job's stored description, with
   CLI verbs they mirror.
2. A description panel on the Apply page that shows the stored text, or
   takes a pasted one when none is stored. The per-operation description
   box goes.
3. `tailor`, `cover-letter` and `answers` refuse when there is no
   description, instead of running against an empty posting.

## Behavior

### The stored description, read and written

The stored description is the existing URL-keyed cache entry. This spec
adds no new store and no new column.

- **Read.** `GET /apply/{selector}/description` answers
  `DescriptionOut {text: str, stored: bool}`. `stored` is true when the
  cache holds a non-empty entry for the job's URL. Otherwise `text` is
  empty and `stored` is false. A cache entry that reads as damaged reads as
  not stored, as `load_cached_description` already does.
- **Write.** `PUT /apply/{selector}/description` with
  `DescriptionIn {text: str}` stores the text as the job's description,
  replacing any entry, through `save_description_cache`. It answers the
  same `DescriptionOut` as the read. The text is stripped of surrounding
  whitespace first.
- Both routes resolve the selector as the brief routes do and require the
  token. Their operations, `description show` and `description set`, are
  allowed on every track kind (spec 094): a description is posting data,
  whatever track holds the row.
- The CLI gets the same two verbs, which the routes mirror (spec 095):
  `harrier description show JOB` prints the text, or
  `no description stored for job JOB` on stderr with exit 1.
  `harrier description set JOB --file PATH` stores the file's text and
  prints `description stored for job JOB`.

A replaced description changes the `description_sha256` that later status
events pin. That is the purpose of that field (spec 079): it says whether
the text judged later is the text judged earlier.

### The Apply page

- A "Job description" panel sits above the operations and loads with the
  page.
- When a description is stored, the panel shows it, read-only and
  collapsed to its first lines, with "Show all" and "Replace". "Replace"
  opens the paste box with the current text in it.
- When none is stored, the panel says "No description was captured for
  this job. Paste it to use it for every operation." and shows the paste
  box with a "Save" button.
- Saving calls the write route. On success the panel shows the stored
  text. On a refusal it shows the API's words (spec 047) and keeps the
  pasted text in the box.
- The per-operation "Job description (optional)" box is removed from the
  resume and evaluate operations. The API keeps their `jd_text` body field,
  unchanged, for any client that sends it.

### No description refuses the operations that need one

`tailor`, `cover-letter` and `answers` need a description. When the run is
given no text and none is stored for the job's URL:

- The CLI verb prints
  `<verb> failed: no job description is stored for job <id>; store one with harrier description set <id> --file PATH`
  on stderr and exits 1. Nothing is generated and no tracker row changes.
- The browser route refuses with 409 before starting a run, with the
  detail `no job description is stored for job <id>; paste one on the Apply page`.
  No run is started and no run input file is written.

Both use one check in the domain. `tailor --no-ai` refuses the same way:
its content plan also ranks by the description.

`evaluate` does not refuse. It evaluates a recorded offer, and it already
runs without a description today.

## Failure modes

| Case | Result |
|---|---|
| The job has no URL (a manual add without one) | Read answers `stored: false`. Write refuses 409: `job <id> has no URL; a description is stored by URL`. The CLI verb exits 1 with the same words. |
| The pasted text is empty or only whitespace | Write refuses 400: `the description is empty`. Nothing is stored. |
| The pasted text is longer than 65,536 bytes in UTF-8 | Write refuses 400 naming the limit. Nothing is stored. |
| The text holds a lone surrogate or a NUL | Write refuses 400 naming the rule, never quoting the text. |
| The store write fails (disk, Storage) | 500 from the route, exit 1 from the CLI, logged by error class only. The previous entry is unchanged: `save_description_cache` writes atomically (spec 007 amendment). |
| Two jobs share one URL | Not possible: URL is unique across every track (spec 092). |
| The selector names no job, or several | 404, as the brief routes answer. |
| A non-default track from the browser | The operation routes keep their 409 (spec 101). The description routes read and write on any track. |
| A stored entry that is damaged | Reads as not stored. The operations refuse as for a missing one. Saving replaces it. |
| Text passed to an operation and a description stored | The passed text is used for that run, as today. The stored one is not changed. |
| Hosted deployment | The cache goes through `files()`, which is the owner's Storage on hosted (spec 107). The routes behave the same. |

No log line carries the description text.

## Acceptance criteria

- [ ] For a job whose URL has a cached description, the read route answers
      `stored: true` and that text; for a job with none, `stored: false`
      and empty text (planned test_the_description_route_reads_the_cache).
- [ ] The write route stores the text so that `load_cached_description`
      for the job's URL returns it, and a second write replaces it
      (planned test_the_description_route_stores_and_replaces).
- [ ] The write route refuses empty text, text over the limit, and a job
      with no URL, and stores nothing in each case
      (planned test_the_description_route_refuses_what_it_cannot_store).
- [ ] `tailor`, `cover-letter` and `answers` with no text and no stored
      description exit 1 with the message above and write no artifact and
      no tracker change (planned test_an_operation_without_a_description_refuses).
- [ ] The resume, cover-letter and answers routes answer 409 with the
      detail above and start no run when no description is stored
      (planned test_an_apply_route_without_a_description_starts_no_run).
- [ ] `evaluate` with no description still starts its run
      (planned test_evaluate_runs_without_a_description).
- [ ] The Apply page shows the stored text when there is one, the paste
      box when there is none, and the stored text after a save
      (planned DescriptionPanel tests in `apps/web`).
- [ ] The resume and evaluate operations on the Apply page no longer show
      a description box (planned ApplyPage test).
- [ ] `harrier description show` and `harrier description set` print and
      store as above (planned test_the_description_verbs).
- [ ] The contract is regenerated and declares both routes and every
      refusal cause (`just contract`).

## Proof / origin

- The cache and its fallback readers: the files named in Problem.
- The Apify field: `descriptionText`, read in `normalize_apify_job`
  (`services/api/src/harrier/sources/apify_linkedin.py`), checked against a
  live dataset from the `curious_coder/linkedin-jobs-scraper` actor on
  2026-10-16.
- Route and verb parity: spec 095. Refusals in the API's words: spec 047.

## Out of scope

- The job poster from the Apify item. Spec 150.
- Descriptions that sources cut short: Greenhouse, Lever, Ashby and
  RemoteOK keep the first 4,000 characters. Replace on the Apply page
  covers a short one by hand. Raising the limit is its own change.
- Fetching a missing description from the posting's page on demand.
- A "has description" marker in the tracker table or the jobs list.
- Storing a description passed with `jd_text` or `--jd-file`. Those stay
  one-run inputs.
- Other Apify fields the importer drops (`postedAt`, `salary`,
  `seniorityLevel`, `employmentType`, `applyUrl`).

## Migration

None. Jobs discovered before this change already have their descriptions
cached and show them. A job with none refuses tailoring until one is
pasted, which is the intended change.

## Least confident

- The 65,536-byte write limit is a choice, not a measured need.
- Refusing `tailor --no-ai` without a description. It could instead run
  untailored with a stated warning.
- Allowing the description verbs on every track kind. An academic track
  tailors from the CLI only (spec 101), so the browser panel is useful
  there only for reading.
