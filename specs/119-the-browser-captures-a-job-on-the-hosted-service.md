---
spec: 119
title: The browser captures a job on the hosted service
status: accepted
approved: yes
milestone: M10
depends: [104, 114]
---

# Spec 119: The browser captures a job on the hosted service

Split from spec 114 on 2026-10-10, when spec 114 was refined (its Open
decision 1). Spec 114 keeps sign-in, the bearer token and the run stream.

Refined 2026-10-18 from the approved stub, together with spec 161 (the
Chrome extension that hands fields to this page) and spec 162 (the
bookmarklets retire). Akin approved this refined text on 2026-10-18,
in the same session, before implementation.

## Problem

Spec 104's Open decision 6 stops serving the capture GET and form post on
the hosted API, and says a later spec gives the SPA a capture page the
bookmarklet navigates to. Until one exists, a hosted owner cannot capture
a job from a job page at all.

The stub assumed the bookmarklet would be what navigates there. On
2026-10-18 Akin chose a Chrome extension instead (spec 161) and chose to
retire the bookmarklets (spec 162). The extension reads the job page and
needs somewhere to send what it read. It holds no session and makes no API
call of its own, so the page that receives the fields is also the page that
signs in, shows the fields, and adds the job.

What spec 114's reading found, still true on `main` at `72066ab`:

- Fields in a query string reach Vercel's request logs. Fields in the
  fragment (`#...`) are never sent to the server.
- The app has no router; it selects views in `apps/web/src/app/App.tsx`
  and keeps the track in the query string (spec 094). A fragment route
  needs no rewrite rule on Vercel.
- Spec 114's code sign-in does not navigate, so the URL, fragment
  included, is unchanged after sign-in. `AuthGate` renders the app only
  once signed in (`apps/web/src/app/AuthGate.tsx`).
- Capture writes to the default track and takes no `track` parameter
  (`services/api/src/harrier_api/deps.py`).

## Scope

One new view in the SPA, the capture page, reached by a fragment. It posts
through the generated client, so it works in both app modes: with spec
114's bearer token on hosted, and with the local token locally. No API
route changes.

## Behavior

### The address

`<web app origin>/#capture?<fields>`, where `<fields>` is a URL-encoded
query string. The names are the fields of `CaptureIn`
(`services/api/src/harrier_api/capture_routes.py`): `title`, `company`,
`location`, `url`, `source`, `description`. Unknown names are ignored. A
missing name reads as empty. `source` defaults to `manual`, as on the
server.

### Opening the page

- When the app renders and the fragment starts with `#capture?`, the main
  area shows the capture page instead of the tracker. The section nav stays
  visible; choosing a section leaves the capture page without writing
  anything.
- Not signed in (hosted): the sign-in screen shows first, as for any
  visit. The fragment is still in the address after code sign-in, so the
  capture page then opens with the same fields.
- When the capture page reads the fragment, it removes it from the address
  with `history.replaceState`, keeping the path and query string (the
  track). Reloading the tab afterwards opens the tracker, not a second
  capture, and the description does not stay in the address bar.

### The form

- Text inputs for title, company, location and link, and a text area for
  the description, each prefilled from the fragment and editable. The
  source is shown as text beneath the form and is sent unchanged.
- The description is cut to 8000 characters when read, the bookmarklets'
  limit. The server cuts it again to 4000 (spec 010); this page does not
  change that.
- The link keeps only an absolute `http:` or `https:` address. Any other
  value (a `javascript:` URL, a relative path) is replaced with empty, and
  the page says the link was left out because it was not a web address.
  This is the same rule spec 142 applies to LinkedIn links.
- Every field renders as text. Nothing from the fragment is ever inserted
  as HTML.
- The page names the track it adds to: the default track, by its name.
- **Add to tracker** is disabled while title or company is blank after
  trimming, and while a request is in flight. **Cancel** returns to the
  tracker and writes nothing.

### Adding

- **Add to tracker** sends `POST /capture/add` through the generated
  client with the six fields as they stand in the form.
- 200: the form is replaced by "Added to <track name>" and a button that
  opens the tracker. Nothing on the page can post again.
- 409 or 400: the server's message is shown (`already in tracker`, the
  focused-track refusal of spec 137, or the missing-field message). The
  form stays, unchanged, so the person can edit and retry or cancel.
- Any other failure, a network error included: the refusal is read with
  the existing reader (`apps/web/src/shared/api/refusal.ts`) and shown;
  the form stays and Add can be pressed again.
- Every result message goes into a live region that is already in the
  page before the message arrives (spec 156).

## Failure modes

- **A page crafts a link to `#capture?...` with fields of its choosing.**
  The form is prefilled and nothing is written until the signed-in owner
  presses Add. A `javascript:` link is dropped. Text renders as text.
- **The session ends between opening the page and pressing Add.** The
  request is refused with 401. If the session is gone, `AuthGate` shows
  the sign-in screen and the form's fields are lost, because the fragment
  was already removed. The person captures again from the posting. Named
  under Honest limitations rather than solved.
- **The same job is captured twice.** The second Add gets the server's 409
  and shows `already in tracker`. No second row.
- **Add pressed twice quickly.** The button is disabled from the first
  press until the response, so one request is sent.
- **The fragment holds only a description (spec 161's selection path).**
  The form opens with title and company empty and Add disabled until the
  person types them.
- **`#capture?` with nothing after it.** The form opens empty. Add stays
  disabled until title and company are typed.
- **No fragment, or a fragment that is not `#capture?`.** The app behaves
  exactly as today.

## Acceptance criteria

Proven by `apps/web/src/pages/capture/fragment.test.ts`,
`apps/web/src/pages/capture/CapturePage.test.tsx`,
`apps/web/src/app/App.test.tsx` and `apps/web/src/app/App.hosted.test.tsx`.

- [x] A fragment `#capture?title=T&company=C&location=L&url=https%3A%2F%2Fexample.com%2Fjob&source=wttj_manual&description=D`
      opens the capture page with each field prefilled and the source
      shown (fragment.test.ts::"a capture fragment yields every field",
      CapturePage.test.tsx::"the form shows every field and the source",
      App.test.tsx::"a capture fragment opens the capture page instead of the tracker")
- [x] After the page reads the fragment, `location.hash` is empty and
      the query string is unchanged
      (CapturePage.test.tsx::"reading the page removes the fragment and keeps the track")
- [x] A signed-out visit with a capture fragment shows sign-in, and after
      sign-in the capture page opens with the same fields and adds with
      the bearer token (App.hosted.test.tsx::"a capture survives sign-in")
- [x] `url=javascript%3Aalert(1)` leaves the link empty and shows the
      left-out message, as does a relative path or an `ftp:` address
      (fragment.test.ts::"a link that is not an http address is left out",
      CapturePage.test.tsx::"the left-out link is named")
- [x] A description of 9000 characters is held as its first 8000
      (fragment.test.ts::"the description is cut to 8000 characters")
- [x] A missing source reads as `manual`, and a fragment that is not a
      capture is not read (fragment.test.ts::"a missing source reads as manual",
      ::"a fragment that is not a capture is not read")
- [x] Add is disabled with a blank title, with a blank company, and while
      a request is in flight
      (CapturePage.test.tsx::"add waits for a title and a company",
      ::"add is disabled while the request is in flight")
- [x] Add sends one `POST /capture/add` with the form's six fields
      through the generated client
      (CapturePage.test.tsx::"add posts the form once with its six fields")
- [x] A 200 shows "Added to" with the default track's name and no Add
      button (CapturePage.test.tsx::"an add names the track it went to and offers no second add")
- [x] A 409 shows the server's message and keeps the form, and a refusal
      with no message is read by the shared reader
      (CapturePage.test.tsx::"a duplicate shows the server's message and keeps the form",
      ::"a refusal without a message is read by the refusal reader")
- [x] Cancel and choosing a section leave the page and write nothing
      (CapturePage.test.tsx::"cancel writes nothing",
      App.test.tsx::"cancel on the capture page opens the tracker",
      ::"choosing a section leaves the capture page")
- [x] The result message goes into a status region present before it
      (CapturePage.test.tsx::"the status region is there before any message")
- [x] With no capture fragment, the tracker opens as before
      (App.test.tsx::"renders the shell and the tracker fed by /jobs")
- [x] No field from the fragment reaches `dangerouslySetInnerHTML` or an
      `href`: the page renders each one as an input value or text
      (CapturePage.test.tsx::"no field reaches the page as markup")
- [x] `pnpm --filter @harrier/web type-check`, lint and test pass; `just
      contract` shows no diff

## Proof / origin

- `specs/114-the-browser-signs-in-to-the-hosted-api.md`: "What spec 119
  inherits" and Open decision 1.
- `specs/104-the-hosted-api-authenticates-every-request.md`: Open decision
  6 and what the hosted API serves.
- `specs/094-the-browser-works-in-a-chosen-search-track.md`: the track in
  the query string.
- `specs/142-a-linkedin-link-never-carries-an-unsafe-scheme.md`: the
  link scheme rule.
- `specs/156-a-status-message-goes-into-a-live-region-that-was-already-there.md`.
- `apps/web/src/app/App.tsx`, `apps/web/src/app/AuthGate.tsx`,
  `services/api/src/harrier_api/capture_routes.py`,
  `services/api/src/harrier_api/deps.py`, read on `main` at `72066ab`.
- Akin's choices in the session of 2026-10-18: hosted target, the
  extension hands off to the web app, the bookmarklets retire.

## Out of scope

- The extension that opens this page: spec 161.
- Retiring the bookmarklets and the local GET confirmation route: spec 162.
- Sign-in, the bearer token and the run stream: spec 114.
- Capturing into a chosen track. The page names the default track; it
  does not offer a choice.
- Any change to an API route, `CaptureIn`, or `add_captured_job`.
- Keeping the fields through a session that ends mid-capture.

## Honest limitations

- A session that ends between opening the page and pressing Add loses the
  fields. Spec 114's sessions refresh themselves, so this needs a sign-out
  in another tab or a revoked session. Keeping a copy in `sessionStorage`
  would fix it and is left out to keep fields out of storage.
- `replaceState` changes the current tab's entry. The browser's history
  list may still keep the address as first visited, fragment included;
  that was not checked. It holds a job posting's public fields, not
  personal data.

## Changes to approved specs

- **Spec 119 (the stub).** It said `docs/bookmarklets.md` documents
  hosted variants beside the local ones, and that the local bookmarklets
  do not change. Both are withdrawn: the extension (spec 161) replaces the
  hosted bookmarklet, and spec 162 retires the local ones. That local API
  routes do not change in this spec still holds.

## Migration

None for this spec on its own. The page is new and reached only by its
fragment.
