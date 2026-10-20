# Chrome extension: capture the open job page

The extension in `apps/extension` reads the job page you are looking at when
you click its icon, and opens the web app's capture page (spec 119) in a new
tab with what it read. You check the fields there and click Add. The
extension makes no network request, holds no session and writes nothing to
Harrier itself (spec 161).

## Build

```bash
pnpm --filter @harrier/extension build
```

This writes the unpacked extension to `apps/extension/dist/`, which git
ignores.

## Load it in Chrome

1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and choose `apps/extension/dist`.

After each rebuild, click the reload arrow on the extension's card. Chrome
shows a developer-mode notice for unpacked extensions; that is expected.

## Set the web app address

The first click on the icon opens the options page, because there is no
default address. Enter the web app's origin and click Save:

- the hosted web app, for example `https://harrier.example.com`, or
- the local one, `http://localhost:5173` or `http://127.0.0.1:5173`.

Any other address is refused with a message naming these forms. A path,
query or fragment is refused too: the extension adds `/#capture?...` itself.
Proven by `apps/extension/src/options/options.test.ts`.

## Use it

- **The open page.** Click the Harrier icon on a job page. A capture tab
  opens next to it.
- **Selected text.** Select the posting text, right-click, and choose
  **Add selection to Harrier**. The selection becomes the description and
  the tab's address the link. Type the title and company on the capture
  page.
- **A page Chrome does not let extensions read** (`chrome://` pages, the
  Web Store): the icon shows `!` and its tooltip says "Harrier can't read
  this page". The next click clears it.

## Which reader runs, and the tests that prove it

Each site reader ports its bookmarklet's rules from `docs/bookmarklets.md`,
where the selector reasoning is recorded. The readers live in
`apps/extension/src/reader/sites.ts` and `apps/extension/src/reader/generic.ts`.

| Page | Source label | Proof |
|---|---|---|
| `linkedin.com/jobs/...` | `linkedin_manual` | `apps/extension/src/reader/linkedin.test.ts` |
| `wellfound.com` | `wellfound_manual` | `apps/extension/src/reader/wellfound.test.ts` |
| `welcometothejungle.com` | `wttj_manual` | `apps/extension/src/reader/wttj.test.ts` |
| `hiring.cafe` with a job dialog open | `hiringcafe_manual` | `apps/extension/src/reader/hiringcafe.test.ts` |
| any other page: JSON-LD `JobPosting`, else `h1` and page title | `manual` | `apps/extension/src/reader/generic.test.ts` |

A site reader that finds no title, or throws, hands the page to the generic
reader, and when that throws the capture page opens with only the link
(`apps/extension/src/reader/page.test.ts`). The built `reader.js` hands its
fields back to the extension as its last statement
(`apps/extension/src/reader/inject.test.ts`).

The capture address, the selection, the missing address and the `!` badge
are proven by `apps/extension/src/capture.test.ts`. The four permissions and
the absence of host permissions and content scripts are proven by
`apps/extension/src/manifest.test.ts`. The lint config,
`apps/extension/eslint.config.js`, refuses `fetch`, `XMLHttpRequest` and
`WebSocket` anywhere in the extension's source.

`just check-ts` runs the extension's type-check, lint, format check and
tests beside the web app's.

## Honest limitations

- The test pages are synthetic, modelled on each board's markup as
  `docs/bookmarklets.md` records it. They prove the rules, not that the
  rules still match the live sites. A board redesign passes every test and
  shows up as empty fields on the capture page, before anything is written.
- jsdom lays nothing out, so the tests read `textContent` where Chrome
  reads `innerText`. A rule that depends on the line breaks Chrome lays out
  (LinkedIn's "Company · Location" line) is tested on fixtures that put
  each line on its own line of HTML.
- A career page with neither JSON-LD nor a meaningful `h1` gives little
  beyond its link.
- Unpacked install only: no Chrome Web Store listing, no Firefox or Safari
  build.
