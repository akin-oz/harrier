---
spec: 123
title: The browser shows and sets a posting's Türkiye eligibility
status: accepted
approved: yes
milestone: M9
depends: [094, 095, 122]
---

# Spec 123: The browser shows and sets a posting's Türkiye eligibility

## Problem

Spec 122 records, for every industry row, whether the posting can engage
someone based in Türkiye and how, with the evidence. The API serves it:
`JobOut.eligibility` on every row, `GET` and `POST
/tracker/{selector}/eligibility`, and `POST /ops/eligibility-reassess`. The
browser calls none of them. Spec 122 named this as a known gap against
spec 095, which says every command that works on the operator's data runs
from the browser.

What that costs today:

- **The verdict is invisible where decisions are made.** The tracker table
  shows status, score and next action. A row the rules marked `excluded`
  looks exactly like one marked `confirmed`. The candidate decides whether
  to apply without the one fact spec 122 exists to surface.
- **Finding rows by verdict needs a terminal.** The only way to list the
  excluded or possible rows is to query `GET /jobs` by hand and filter the
  JSON.
- **Correcting a verdict needs a terminal.** `harrier eligibility set` is
  the only way to record the candidate's own verdict, so a wrong rule
  verdict noticed while reviewing a row in the browser stays wrong until
  the candidate switches tools.
- **The evidence is not readable in context.** The excerpt and its source
  URL, which make a verdict auditable, are reachable only through `harrier
  eligibility show`.
- **Reassessment needs a terminal.** After the rules change, `harrier
  eligibility reassess` is the only way to re-run them.

Who it hurts: the candidate, reviewing postings in the browser.

## Scope

Browser only. No route, model, store or rule changes; the contract does not
change.

- `apps/web/src/entities/job/eligibility.ts` (new): the verdict, route and
  origin labels and the verdict order, typed from the generated contract.
- `apps/web/src/entities/job/ui/EligibilityPill.tsx` (new): the row's
  verdict and routes, read-only.
- `apps/web/src/entities/job/JobTable.tsx`: the pill in the Job cell,
  behind a prop the page sets on the default track only, as it sets
  `deadlineLed` on the others.
- `apps/web/src/features/eligibility/EligibilityPanel.tsx` (new): the row
  detail, the history and the candidate's form.
- `apps/web/src/pages/tracker/TrackerPage.tsx`: the verdict filter, and the
  panel wired into the row detail beside the history.
- `apps/web/src/pages/operations/EligibilitySection.tsx` (new) and
  `OperationsPage.tsx`: reassess.
- CSS beside each component, following the files they sit next to.
- Tests beside each file, with invented companies only. Two existing test
  fixtures that build job rows by hand (`app/App.test.tsx`,
  `pages/tracker/TrackerPage.test.tsx`) gain the "no assessment" value,
  since the page now reads it; `pages/operations/OperationsPage.test.tsx`
  gains a stub for the reassess route.

## Behavior

Everything below appears on the default track only. Spec 122's routes run
on the default track only and every other track is academic today, so
another track shows no pill, no filter group, no panel and no section, and
never calls the routes.

### Words

One table, in `entities/job/eligibility.ts`, typed as a `Record` over the
contract's verdict, route and origin unions, so a value the contract adds
is a compile error until it has words.

| Value | Shown as |
|---|---|
| verdict `confirmed` | Türkiye confirmed |
| verdict `possible` | Türkiye possible |
| verdict `unknown` | Türkiye unknown |
| verdict `excluded` | Türkiye excluded |
| route `employee` | employee |
| route `contractor` | contractor or B2B |
| route `eor` | employer of record |
| route `relocation` | relocation |
| origin `rule` | the rules |
| origin `candidate` | your verdict |
| origin `none` | not assessed |

The verdict order everywhere is confirmed, possible, unknown, excluded.

### The pill, in the table

In the Job cell, after the meta line, a pill names the row's current
verdict, followed by its routes when there are any: "Türkiye possible ·
contractor or B2B". It has one visual weight per verdict, and the verdict
is in its text, so color is never the only signal.

- A row with `verdict = unknown` shows no pill. Unknown is the reading of
  most rows and of every row with no assessment; a pill on each would hide
  the ones that say something. The filter below still finds them.
- A row whose current verdict is the candidate's shows "your verdict" in
  the pill's accessible name and a small mark.
- A row with `disagrees = true` shows a second mark, "the rules differ",
  so the candidate can find verdicts the rules no longer agree with.

The pill reads `job.eligibility` from the rows the page already fetched.
It makes no request.

### The filter

A third chip group under the status chips, labelled "Türkiye", with All
and one chip per verdict in the verdict order. Each chip shows the count of
rows with that current verdict across the whole tracker, as the status
chips do. It filters the rows already fetched, combined with the status
chip and the search by AND, in every view (All, Next up, Needs a decision).
The queue views keep the server's order; the filter only removes rows.

The empty message for a filter that matches nothing is the existing "No
jobs match this filter."

### The panel, in the row detail

Beside "History", an "Eligibility" toggle opens a panel. Opening it fetches
`GET /tracker/{selector}/eligibility`; closed, it fetches nothing.

The panel shows, from the row:

- the verdict, the routes ("routes unknown" when there are none), and who
  made it, in the words above;
- the excerpt in quotation marks with the field it came from, when there is
  one;
- the evidence URL as a link that opens in a new tab with `rel="noreferrer"`,
  when there is one and it starts with `http://` or `https://`; any other
  value is shown as text, never as a link;
- when it was checked, and the rules version for a rule assessment;
- when `disagrees` is true: "The rules' latest assessment differs from
  yours. Yours stays current." and the rules' verdict, from the history.

Then the history, newest first, one line per assessment: when it was
written, the verdict and routes, who made it, and the excerpt. The matched
phrases are shown on a rule assessment's line, collapsed by default.

### The candidate's form

In the panel, on a writable track (not archived), a form records the
candidate's verdict through `POST /tracker/{selector}/eligibility`:

- verdict: one radio per verdict, none selected at first;
- routes: one checkbox per route;
- evidence URL: a text field;
- excerpt: a text area with `maxLength` 300 and a count of characters left;
- checked: a date field, optional; empty sends no `checked_at`, so the
  server uses now.

The browser does not restate the server's rules. "Confirmed and excluded
need evidence" and every other refusal come back as a 409 with the
domain's sentence, which the form shows in an alert, keeping what the
candidate typed. A 404, a 503 and a refused token (403) show their
sentences the same way: the API's 403 carries its own sentence
(`harrier_api.localauth.require_token`).

On success the form clears, the panel shows the new current verdict, and
the page's job queries for the track are invalidated, so the pill and the
filter counts update without a reload.

### Reassess, on the operations page

A section "Türkiye eligibility" after Reconsideration, in the shape that
section uses: a report first, then an apply that is offered only after a
report has come back.

- "Report what would be assessed" sends `POST /ops/eligibility-reassess`
  with `apply: false` and shows the report: assessed of examined,
  unchanged, rejected and skipped, with no stored description, the count
  per verdict in the verdict order, and, when nonzero, how many rows have
  the candidate's verdict and on how many the rules now differ.
- "Assess what the report found" sends `apply: true` and shows the report
  it returns. The rows may have changed between the two requests; the
  second report is what was written, and the section says so: "Written.
  Counts are from this run."
- The route answers synchronously, so this section shows a pending state
  while waiting and does not use the run stream.

A verdict key in `by_verdict` that is not one of the four is not shown.
`by_verdict` is a free string map in the contract (spec 122's review left
it so), and this section is its first reader.

## Failure modes

- **The history request fails** (404, 503, network). The panel shows the
  error in an alert and a retry button; the row's current verdict, which
  came with the row, still shows.
- **The candidate submits a firm verdict without evidence.** The server
  refuses with 409; the form shows the sentence and keeps the input.
- **A refused token** (403). The form and the reassess section show the
  API's sentence and change nothing.
- **The table is stale after a write elsewhere** (the CLI, another tab).
  The pill and counts update on the next fetch of the jobs query, as every
  other column does. No polling is added.
- **Report, then the data changes, then apply.** Apply runs the rules again
  and reports what it wrote; the section never presents the first report's
  numbers as written.
- **An evidence URL that is not http or https.** Shown as text, never as a
  link, whatever the store holds.
- **A long excerpt.** It wraps inside the panel; the table shows no
  excerpt, so row height does not change.
- **An academic or other non-default track.** Nothing from this spec
  renders and no eligibility route is called.
- **An archived default track.** The pill, filter and panel read; the form
  is not shown. (The default track cannot be archived today; the rule
  matches the page's existing `writable` flag.)
- Must not introduce: a request per row to show the pill; a client-side
  copy of the server's evidence rule; a sort or reorder by verdict; a link
  from a value that is not http or https; a call to an eligibility route
  from a non-default track.

## Acceptance criteria

Tests beside each file under `apps/web/src/`, run by `pnpm --filter
@harrier/web test`, cited as file and test title. Every company is
invented.

Words and pill

- [x] Every verdict, route and origin in the contract has words; the record
      is typed so a missing one fails `tsc`
      (`apps/web/src/entities/job/eligibility.test.ts`: "words cover the
      contract")
- [x] Only http and https become links
      (`apps/web/src/entities/job/eligibility.test.ts`: "only http and https
      are links")
- [x] A `possible` row with route `contractor` shows "Türkiye possible ·
      contractor or B2B"; an `unknown` row shows no pill
      (`apps/web/src/entities/job/ui/EligibilityPill.test.tsx`: "the pill
      names the verdict and routes", "an unknown verdict shows no pill")
- [x] A row with the candidate's verdict and `disagrees` shows both marks,
      in words, so both are in the accessible name
      (`apps/web/src/entities/job/ui/EligibilityPill.test.tsx`: "the pill
      marks your verdict and a disagreement")
- [x] Rendering the table makes no request, shows no excerpt, and shows no
      pill unless the page asks for it
      (`apps/web/src/entities/job/JobTable.test.tsx`: "the table fetches no
      eligibility", "the pill is absent unless the page asks for it")

Filter

- [x] The Türkiye chips show per-verdict counts over the whole tracker and
      filter the rows, combined with status and search
      (`apps/web/src/pages/tracker/TrackerPage.test.tsx`: "the verdict
      filter counts and narrows")
- [x] In "Next up" the filter keeps the server's order
      (`apps/web/src/pages/tracker/TrackerPage.test.tsx`: "the verdict
      filter keeps the queue order")
- [x] An academic track shows no Türkiye chips, no pill and no panel, and
      calls no eligibility route
      (`apps/web/src/pages/tracker/TrackerPage.test.tsx`: "another track
      shows no eligibility")

Panel and form

- [x] Opening the panel fetches the history once, without the token; closed,
      it fetches nothing
      (`apps/web/src/features/eligibility/EligibilityPanel.test.tsx`: "the
      panel reads history on open")
- [x] The panel shows the excerpt, the field, the checked time, the rules
      version and an http evidence link with `rel="noreferrer"`; a non-http
      value renders as text (`EligibilityPanel.test.tsx`: "the panel links
      only http evidence")
- [x] A disagreement shows the sentence and the rules' verdict
      (`EligibilityPanel.test.tsx`: "the panel explains a disagreement")
- [x] Submitting sends the verdict, routes in their order, URL and excerpt,
      the date only when set, with the token, then invalidates the track's
      job queries and clears the form (`EligibilityPanel.test.tsx`: "the
      form sends what was entered and refreshes", "the form sends a checked
      date when one is set")
- [x] A 409 shows the domain's sentence and keeps the input; a refused token
      shows the API's sentence (`EligibilityPanel.test.tsx`: "the form shows
      refusals and keeps input", "a refused token shows the API's sentence")
- [x] A refused history read shows its error, keeps the row's verdict and
      can be retried (`EligibilityPanel.test.tsx`: "a refused history read
      shows the error and can be retried")
- [x] The form is absent on an archived track (`EligibilityPanel.test.tsx`:
      "the form is absent when the track is read only")

Operations

- [x] Reporting sends `apply: false` with the token and shows the counts in
      the verdict order; the apply button appears only after a report
      (`apps/web/src/pages/operations/OperationsPage.test.tsx`: "reassess
      reports before it writes")
- [x] Applying sends `apply: true` and labels the result as written
      (`OperationsPage.test.tsx`: "reassess apply shows what was written")
- [x] A verdict key outside the four is not shown
      (`OperationsPage.test.tsx`: "reassess ignores an unknown verdict key")
- [x] A refused reassess shows the API's words (`OperationsPage.test.tsx`:
      "a refused reassess shows the API's words")

Whole

- [x] `pnpm type-check`, `pnpm lint`, `prettier --check` and the web tests
      pass under Node 22; `just contract` produces no diff
- [x] `fsd-reviewer` finds no layer violation: the entity renders no
      feature, the page wires the panel through `renderDetails`

## Proof / origin

- No old-repo origin: the old GUI had no eligibility. The data and the
  rules are spec 122's; this spec adds no behavior to them.
- The report-then-apply shape is `ReconsiderSection.tsx` (spec 031, routed
  by spec 095).
- The read-on-open detail is `JobHistory.tsx` (spec 095).
- The chip filter over one fetch is `TrackerPage.tsx` (specs 026, 042).
- The page wiring an entity's slot to a feature is `JobTable.tsx`'s
  `renderDetails` and `renderActions` (spec 042, FSD).
- Showing the domain's refusal sentence is spec 082's `refusalMessage`.

## Out of scope

- **Any API, store, rule or contract change.** If a gap appears that needs
  one, it is an amendment to spec 122, not this spec.
- **Ordering by verdict.** Queue order stays the server's (spec 122
  deferred it).
- **A server-side filter parameter on `GET /jobs`.** The page filters the
  set it already fetches.
- **Typing `by_verdict` in the contract.**
- **The digest, Telegram and the run panel.**
- **Treating "C2C" as a contract route.** That is a rule change and belongs
  to spec 122.
- **Company-level eligibility and a company watchlist.**
- **Non-default tracks.** They get eligibility when spec 122's routes do.

## Migration

None. The data exists since spec 122; this spec only shows it and adds the
browser's way to write it.

## Amendments in the implementing change

1. **A refused token shows the API's sentence**, not a separate token
   message: the API's 403 carries its own `detail`, which the shared
   `refusalMessage` shows, as it does for every other refusal.
2. **Test citations name the file and the test title.** The web tests are
   titled in words, as the rest of the web suite is.

## Decided in review (2026-10-11)

1. **No pill for `unknown`.** The pill shows only for confirmed, possible
   and excluded, so they stand out; the filter still finds unknown rows.
2. **Reassess lives on the operations page**, beside Reconsideration.
