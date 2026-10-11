---
spec: 119
title: The browser captures a job on the hosted service
status: proposed
approved: no
milestone: M10
depends: [104, 114]
---

# Spec 119: The browser captures a job on the hosted service

Split from spec 114 on 2026-10-10, when spec 114 was refined (its Open
decision 1). Spec 114 keeps sign-in, the bearer token and the run stream.

## Problem

Spec 104's Open decision 6 stops serving the capture GET and form post on
the hosted API, and says a later spec gives the SPA a capture page the
bookmarklet navigates to. Until one exists, a hosted owner cannot capture
a job from a job page at all.

What spec 114's reading found, for this spec to start from:

- The bookmarklets navigate with `window.open` to
  `http://localhost:'+PORT+'/capture/add?` plus the fields as a query
  string, the description cut to 8000 characters (`docs/bookmarklets.md:49`
  and the three single-site variants after it). A hosted variant needs the
  SPA's origin in place of `localhost:PORT`.
- Fields in a query string reach Vercel's request logs. Fields in the
  fragment (`#...`) are never sent to the server.
- The app has no router; it selects views in `App.tsx` and keeps the track
  in the query string (spec 094). A fragment route needs no rewrite rule
  on Vercel.
- If the person is not signed in, the captured fields must survive the
  sign-in page. With spec 114's code sign-in there is no redirect, so
  component state holds them.
- Capture writes to the default track and takes no `track` parameter
  (`services/api/src/harrier_api/deps.py:43-45`).

## Scope

Stub created by spec 114's refinement to sequence the hosted build. It is
refined into a real spec, with behavior and failure modes, before approval
is asked for.

## Acceptance criteria

Headline, to be expanded into checkable criteria when refined: hosted, a
bookmarklet opens a capture page in the SPA at the SPA's own origin,
carrying the job's fields in the URL fragment, never the query string. The
page keeps the fields through sign-in, shows them to the owner, and posts
`POST /capture/add` through the generated client, so spec 114's bearer
middleware applies. `docs/bookmarklets.md` documents the hosted variants
beside the local ones. Locally, capture and the local bookmarklets do not
change.

## Proof / origin

- `specs/114-the-browser-signs-in-to-the-hosted-api.md`: "What spec 119
  inherits" and Open decision 1.
- `specs/104-the-hosted-api-authenticates-every-request.md`: Open decision
  6 and what the hosted API serves.
- `specs/094-the-browser-works-in-a-chosen-search-track.md`: the track in
  the query string.
- `docs/bookmarklets.md`, `apps/web/src/app/App.tsx`,
  `services/api/src/harrier_api/deps.py`, read on `main` at `44ac0d7`.

## Out of scope

- Sign-in, the bearer token and the run stream: spec 114.
- Capturing into a chosen track.
- Any change to the local capture routes (spec 035).
