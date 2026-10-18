---
spec: 161
title: A Chrome extension hands the open job page, or selected text, to the web app
status: accepted
approved: yes
milestone: M10
depends: [119]
---

# Spec 161: A Chrome extension hands the open job page, or selected text, to the web app

## Problem

Capturing a job from a page the owner is reading has one path today: the
bookmarklets in `docs/bookmarklets.md`. They fall short in four ways.

- **They reach only the local API.** Each one opens
  `http://localhost:8000/capture/add`. The hosted service does not serve
  that GET (spec 104), so a hosted owner cannot capture from a job page at
  all. Spec 119 gives the web app a capture page; something has to read the
  job page and open it.
- **They are untested.** Each is a single line of about two thousand
  characters pasted into a bookmark. The selectors are dated April 2026 in
  the doc. When LinkedIn, Wellfound, Welcome to the Jungle or hiring.cafe
  change their markup, nothing fails until a capture comes back empty.
- **Any other site gets the first `<h1>` and the page title.** Company
  career pages on Greenhouse, Lever, Ashby and similar boards are common
  sources, and the bookmarklet reads almost nothing from them.
- **Text that is not a job page cannot be captured.** A posting quoted in
  a newsletter, a chat message, or a page with unusual markup has no path
  in, short of typing every field into curl.

## Scope

A Manifest V3 Chrome extension in a new workspace package, `apps/extension`
(package `@harrier/extension`). It reads the active tab when the owner asks
it to and opens spec 119's capture page in a new tab with what it read. It
makes no network request, holds no session, and never writes to Harrier
itself: the web app does the sign-in, the review and the add.

Installed by loading the built folder unpacked. No Chrome Web Store
listing.

## Behavior

### Install and setting

- `pnpm --filter @harrier/extension build` writes the extension to
  `apps/extension/dist/`. The owner loads that folder with **Load
  unpacked** on `chrome://extensions`. A new doc, docs/extension.md,
  gives these steps.
- The extension has one setting, on its options page: the web app address.
  It accepts an `https://` origin, or `http://localhost` or
  `http://127.0.0.1` with an optional port (spec 119's page works in local
  mode too). Anything else is refused with a message naming the accepted
  forms. The value is stored with `chrome.storage.local`. There is no
  default, so no deployment address is committed.
- Permissions: `activeTab`, `scripting`, `contextMenus`, `storage`. No
  `host_permissions` and no content scripts declared in the manifest. The
  extension can read a page only for the tab, and at the moment, the owner
  clicks it.

### Capturing the open page

The owner clicks the extension's toolbar icon.

1. The extension injects its reader into the active tab with
   `chrome.scripting.executeScript` and receives the six capture fields.
2. It opens a new tab, next to the current one, at
   `<web app address>/#capture?<fields>` (spec 119). The fields are
   URL-encoded, the description cut to 8000 characters.

The reader picks a site reader by the page's hostname and path. Each one
ports the matching bookmarklet's rules, cited to `docs/bookmarklets.md`
as of `72066ab`:

| Page | `source` | Reads |
|---|---|---|
| `linkedin.com/jobs/...` | `linkedin_manual` | title and company from the page title, company link fallback, location from the "Company · Location" text, description from the job details, link without its query string |
| `wellfound.com` | `wellfound_manual` | the `JobListingSlideIn` panel: its `h1`, the avatar's company name, the pill row's location, `#job-description` |
| `welcometothejungle.com` | `wttj_manual` | the job metadata block's title and company, the location and remote chips, the description section, page title and URL slug fallbacks |
| `hiring.cafe` with a job panel open | `hiringcafe_manual` | the dialog's `h2`, the `@ Company` span, the map-pin location, the `/viewjob/` link, the `article` |
| anything else | `manual` | the generic reader, below |

The generic reader, used for every other page and when a site reader
finds no title:

1. A `schema.org` `JobPosting` in a `<script type="application/ld+json">`
   block, at the top level or inside `@graph`: `title`,
   `hiringOrganization.name`, the first `jobLocation` address locality and
   country, `Remote` when `jobLocationType` is `TELECOMMUTE`, and
   `description` converted from HTML to text.
2. Otherwise the first `<h1>` as the title, or the page title, with the
   bookmarklet's "Title - Company" split of the page title for the company.
3. The link is the page address.

A field the reader cannot find is sent empty. The capture page opens
regardless, and the owner fills what is missing.

### Capturing selected text

The owner selects text, right-clicks, and chooses **Add selection to
Harrier**. The item appears only when text is selected.

- The description is the selection, read with `window.getSelection()` in
  the tab so line breaks survive. When that injection fails (a PDF viewer,
  a frame the extension cannot reach), it falls back to the context menu's
  `selectionText`.
- The link is the tab's address when it is `http:` or `https:`, otherwise
  empty. The source is `manual`. Title, company and location are empty:
  the owner types them on the capture page, which keeps Add disabled until
  title and company are filled (spec 119).

### The field shape

The six field names are typed from `CaptureIn` in `@harrier/contract`. A
field renamed or removed on the server fails the extension's type-check.

### Gates

The `check-ts` and `gate` recipes in the justfile run the extension's
type-check, lint, format check and tests beside the web app's. CI already
runs `just check-ts`, so no workflow changes.

## Failure modes

- **No web app address set.** The click opens the options page with a
  message asking for the address. Nothing is captured; the owner clicks
  again after saving it.
- **A page the extension cannot read** (`chrome://` pages, the Chrome Web
  Store, another extension's page). `executeScript` throws. The icon shows
  a `!` badge on that tab and its tooltip reads "Harrier can't read this
  page". No tab opens. The badge clears on the next click.
- **A site reader throws on markup it did not expect.** The error is
  caught and the generic reader runs. If that throws too, the capture page
  opens with only the link.
- **The site changed its markup.** Fields come back empty or wrong. The
  owner sees them on the capture page before anything is written, so the
  failure is a form to correct, not a bad row. See Honest limitations.
- **hiring.cafe with no job panel open.** The site reader finds no dialog
  and the generic reader runs.
- **A page's JSON-LD is malformed or names no `JobPosting`.** It is
  skipped and step 2 of the generic reader runs.
- **The selection is empty after trimming** (whitespace only). The
  capture page opens with only the link.
- **The same job is captured twice.** The capture page shows the server's
  `already in tracker` (spec 119).

## Acceptance criteria

Planned tests under apps/extension/src/, run by vitest with jsdom against
synthetic HTML fixtures. Fixtures may use real board and company names and
carry no real person, posting text, or application content. Cited as
planned until they exist.

- [ ] A synthetic LinkedIn job page yields its title, company, location,
      description, `linkedin_manual`, and its link with the query string
      removed (planned test_linkedin_page_yields_its_fields)
- [ ] A LinkedIn page whose title has two parts takes the company from
      the company link (planned test_linkedin_two_part_title_uses_the_company_link)
- [ ] A synthetic Wellfound split pane reads the slide-in panel, not the
      search bar `h1` (planned test_wellfound_reads_the_panel_not_the_search_bar)
- [ ] A synthetic WTTJ page yields its fields, and falls back to the page
      title and slug when the metadata block is missing
      (planned test_wttj_page_yields_its_fields, planned test_wttj_falls_back_to_title_and_slug)
- [ ] A synthetic hiring.cafe page with its dialog open yields the
      `/viewjob/` link; with no dialog the generic reader runs
      (planned test_hiringcafe_reads_the_open_dialog, planned test_hiringcafe_without_a_dialog_is_generic)
- [ ] A page with a `JobPosting` in JSON-LD, including one inside
      `@graph`, yields title, company, location and plain-text description
      (planned test_json_ld_job_posting_yields_its_fields)
- [ ] A page with malformed JSON-LD falls back to its `h1`
      (planned test_malformed_json_ld_falls_back_to_h1)
- [ ] A site reader that throws is replaced by the generic reader
      (planned test_a_throwing_site_reader_falls_back)
- [ ] The opened address is the configured origin plus `#capture?` and
      the URL-encoded fields, with the description cut to 8000 characters,
      and nothing in a query string (planned test_the_capture_address_carries_fields_in_the_fragment)
- [ ] A selection becomes the description, the tab's http(s) address the
      link, `manual` the source, and the other fields are empty
      (planned test_a_selection_becomes_the_description)
- [ ] With no address set, the click opens the options page and no capture
      tab (planned test_no_address_opens_the_options_page)
- [ ] A refused injection sets the `!` badge and opens no tab
      (planned test_an_unreadable_page_sets_the_badge)
- [ ] The options page refuses `http://example.com` and accepts
      `https://example.com` and `http://localhost:5173`
      (planned test_the_address_must_be_https_or_localhost)
- [ ] The manifest requests exactly `activeTab`, `scripting`,
      `contextMenus` and `storage`, and no host permissions or content
      scripts (planned test_the_manifest_asks_for_four_permissions)
- [ ] The extension source makes no `fetch`, `XMLHttpRequest` or
      `WebSocket` call
- [ ] `just check-ts` runs the extension's type-check, lint, format check
      and tests, and passes
- [ ] docs/extension.md gives the build, load and setting steps and names
      the tests above as the proof of each reader

## Proof / origin

- `docs/bookmarklets.md` at `72066ab`: every site rule this ports, with
  its selector rationale.
- `services/api/src/harrier_api/capture_routes.py`: `CaptureIn`, the
  field names.
- `specs/119-the-browser-captures-a-job-on-the-hosted-service.md`: the
  capture page and its fragment.
- `specs/010-batch-imports-and-capture.md`: the source labels and the
  manual-add path.
- Akin's choices in the session of 2026-10-18: hosted target, typed title
  and company for a selection, unpacked install, hand-off to the web app,
  bookmarklets retired (spec 162).

## Out of scope

- Retiring the bookmarklets and the local GET confirmation route: spec 162.
- The capture page itself: spec 119.
- The extension calling the API, signing in, or holding any token.
- Filling title and company from a selection by an LLM or by heuristics.
- A Chrome Web Store listing, Firefox, Safari or Edge packaging.
- Choosing a track. Capture goes to the default track (spec 119).
- Reading a page without a click: no content scripts, no badge showing
  whether a page is already tracked.
- Capturing several tabs, or a list of search results, in one action.
- Any change to the shared screening path or `add_captured_job`.

## Honest limitations

- The fixtures are synthetic pages modelled on each site's markup as the
  bookmarklet doc records it. They prove the rules, not that the rules
  still match the live sites. A site redesign passes every test and shows
  up as empty fields on the capture page.
- LinkedIn and the other boards are read the way the bookmarklets read
  them: the page the owner is already viewing, once, on the owner's click.
  There is no crawling. Whether a board's terms permit even that is not
  assessed here.
- An unpacked extension shows Chrome's developer-mode notice and must be
  reloaded by hand after each rebuild.
- JSON-LD covers boards that publish it. A career page with neither
  JSON-LD nor a meaningful `h1` gets little beyond its link.

## Classification

`apps/extension/**` is public source, the class of every tracked path not
in `never_in_git` (`config/data-classification.json`). The build output
`apps/extension/dist/` is ignored by the existing `dist/` rule in
`.gitignore`, as `apps/web/dist/` is. Neither file changes.

## Migration

Build the extension, load it unpacked, and set the web app address once
on its options page. The bookmarklets keep working until spec 162 lands.
