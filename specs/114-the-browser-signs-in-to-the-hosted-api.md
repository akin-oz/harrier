---
spec: 114
title: The browser signs in to the hosted API
status: accepted
approved: yes
milestone: M10
depends: [006, 035, 047, 094, 104]
---

# Spec 114: The browser signs in to the hosted API

Akin approved the stub in chat on 2026-10-10; the frontmatter flip reached main inside PR #222, whose message does not say so, and this line records it. Refined 2026-10-10 after approval of the stub. This text is an amendment
to the approved stub and goes to Akin before implementation.

## Problem

Spec 104 makes the hosted API refuse any request without a verified
Supabase token. The SPA in `apps/web` has no way to get one: it sends the
local API token of spec 035, assumes the API shares its origin, and reads
run events with `EventSource`, which cannot send an `Authorization`
header. Spec 104's Open decision 8 put the browser's half here.

What the SPA does today, read from this checkout:

- **One origin, fixed.** The client's base URL is `/api` on the page's own
  origin (`apps/web/src/shared/api/client.ts:10`). Vite proxies `/api` to
  `127.0.0.1:8000` in development (`apps/web/vite.config.ts:6-13`), and the
  API strips the prefix when it serves the built SPA itself
  (`ApiPrefixMiddleware`, `services/api/src/harrier_api/app.py:1441`, mounted at
  `:1625`). Nothing in `apps/web/src` reads `import.meta.env`
  (`grep -rn "import.meta.env" apps/web/src` finds nothing), so there is no
  build-time setting to point the SPA anywhere else.
- **The local token.** The client fetches `GET /session` once and sends
  its value as `X-Harrier-Token` on every state-changing request and on
  eight read patterns (`client.ts:12`, `:31-47`, `:54-68`, `:75-84`).
  Hosted, `GET /session` is not served (spec 104, "What the hosted API
  serves"), and spec 104's CORS list does not allow `X-Harrier-Token`, so
  a hosted request carrying it fails its preflight.
- **`EventSource` streaming.** `useRunStream` takes an `EventSourceFactory`
  (`apps/web/src/features/runs/useRunStream.ts:11`, `:51`) and opens a
  hard-coded relative URL, `/api/runs/${id}/events` (`:80`), outside the
  generated client. Four components default the factory to
  `new EventSource(url)`: `RunPanel.tsx:18`, `ApplyPage.tsx:169`,
  `InboxPage.tsx:46`, `OperationsPage.tsx:30` and `SettingsPage.tsx:33` (counted with
  `grep -rn "new EventSource" apps/web/src`, test files excluded). Ten
  files call `useRunStream(` outside its own module and its tests
  (`grep -ln "useRunStream(" -r apps/web/src | grep -v test`, which lists
  eleven, one of them `useRunStream.ts`). Four test files stub the factory
  with a `FakeEventSource` (`grep -ln FakeEventSource -r apps/web/src`).
  MDN: the `EventSource()` constructor's only option is `withCredentials`;
  it sends no request headers (Proof / origin).
- **No identity.** There is no sign-in, no sign-out, and the query cache
  (`apps/web/src/app/App.tsx:27-31`) holds whatever the API last answered,
  with no notion of whose it was.
- **A count that would lie.** Spec 104 makes hosted `GET /health` return
  `job_count: null`. The health badge renders `job_count ?? 0`
  (`apps/web/src/features/health/HealthBadge.tsx:55`), so every hosted
  page would read "0 jobs".

Three accepted documents still say the SPA streams with `EventSource`:
ADR-004 (`docs/adr/ADR-004-long-running-work.md:54-55`), ADR-005
(`docs/adr/ADR-005-api-contract-seam.md:33-34`) and spec 006
(`specs/006-run-manager-and-sse.md:77-78`). Spec 104's Open decision 4,
which Akin took on 2026-10-10, replaced that with `fetch` in both modes.
The ADRs need a note saying so, or two documents disagree.

## Scope

This spec is the SPA's side of hosted authentication. The server side is
spec 104. The hosted capture page is split out into spec 119 (Open
decision 1); what it inherits from this spec is written under "What spec
119 inherits" so nothing found here is lost.

1. **Two modes, chosen at build time.** `VITE_HARRIER_AUTH=supabase` builds
   the hosted SPA; unset or empty builds the local SPA of today. Hosted
   needs three more variables, validated when Vite starts, so a bad hosted
   build fails at `pnpm build` (Behavior, "Configuration").
2. **Sign-in, hosted only,** with `supabase-js` and an emailed one-time
   code (Open decision 2), in a sign-in page the app shows until a
   session exists.
3. **The bearer token on every API call** through the generated client's
   middleware in `shared/api/client.ts`, so no call site handles a token.
   Hosted, the client never sends `X-Harrier-Token` and never requests
   `GET /session`. Local mode is unchanged.
4. **Refusals handled once.** A 401 `expired` refreshes the session and
   retries the request once; any other 401, a second 401, and a 403
   `anonymous` end the session locally and show the sign-in page. A 503
   `keys_unavailable` never signs anyone out.
5. **The run stream with `fetch` in both modes,** through the generated
   client with `parseAs: "stream"`, so the token middleware applies to it
   like every other call. The stream reconnects with `Last-Event-ID` when
   the server ends it at token expiry (spec 104) and after a network
   error. `EventSource` leaves the codebase, and a lint rule keeps it out.
6. **Sign-out** ends this browser's session, clears the query cache, and
   aborts every open stream. A change of signed-in user clears the cache
   too.
7. **The health badge** shows no job count when the API reports none.
8. **Dependencies.** `@supabase/supabase-js` is added to
   `apps/web/package.json` at the current 2.x release when implemented,
   pinned by `pnpm-lock.yaml`. It is loaded only in hosted mode (Open
   decision 7).
9. **Documents.** ADR-004 and ADR-005 each gain an "Amended (spec 114)"
   note; `docs/architecture.md`'s hosted section names the SPA's hosted
   mode, its variables and this spec.

No contract change. Spec 104 declares `AuthRefusalOut`, the 401, the 501
and the bearer scheme; this spec only reads the generated types. Spec 104
lands first.

Classification: every new file sits under `apps/web/src/`, which
`config/data-classification.json` already classifies as public (public is
the complement of `never_in_git`). This spec adds no env file: hosted
values come from the build's environment (Vercel's project settings,
provisioned by spec 110). This is checked before implementation, and a
path the table does not cover stops the change.

## Behavior

### Configuration

Vite exposes to client code only variables prefixed `VITE_`, replaces them
statically at build time, and gives variables already in the process
environment priority over `.env` files (Vite env guide, Proof / origin).

| Variable | Hosted value | Use |
|---|---|---|
| `VITE_HARRIER_AUTH` | `supabase` | Turns hosted mode on. Unset or empty: local mode. Any other value is refused. |
| `VITE_SUPABASE_URL` | `https://<ref>.supabase.co` | The Supabase project. Must be `https://` with no path, query or fragment. |
| `VITE_SUPABASE_PUBLISHABLE_KEY` | `sb_publishable_...` or the legacy anon key | The project's public client key (Open decision 9). |
| `VITE_HARRIER_API_URL` | `https://<api host>` | The hosted API's origin. Must be `https://` with no path, query or fragment. |

These four are exactly what ADR-013 decision 7 lets Vercel hold: the
Supabase URL, the client key and the API URL, plus a mode switch that is
not a secret. Local mode ignores the last three, so a stray
`VITE_SUPABASE_URL` cannot put the local SPA into a half-hosted state.

One pure function, `readAppMode(env)` in
`apps/web/src/shared/config/mode.ts`, turns these into either
`{ kind: "local" }` or `{ kind: "hosted", supabaseUrl, publishableKey,
apiUrl }`, or throws naming the variable. `vite.config.ts` calls it with
`loadEnv(mode, <the directory of the config file>, "VITE_")`, so `pnpm build`
and `pnpm dev` refuse a bad hosted configuration before bundling. The app
calls it again at start with the four variables read by name, never
`import.meta.env` as a whole, which Vite copies into the bundle with every
`VITE_` variable in the build's environment; a throw there renders a
configuration error page and makes no request.

The key check refuses a value beginning `sb_secret_`, and a JWT whose
payload `role` is `service_role`, naming the variable and never printing
the value, in either mode: Vite puts the key variable's value in the
bundle whether or not the local app reads it. Supabase says a secret key
must never be in a browser and
answers 401 to one used from a browser user agent (API keys guide), but a
secret key in a bundle is already leaked to everyone who loads the page,
so the build refuses it before it ships.

### Where it lives

The layers are ADR-001's and the boundaries lint
(`apps/web/eslint.config.js:21-45`) already enforces them: app may import
every layer, pages import widgets, features, entities and shared, and
shared imports only shared.

| Path | Layer | Holds |
|---|---|---|
| `shared/config/mode.ts` | shared | `readAppMode`, the variable checks |
| `shared/auth/session.ts` | shared | the Supabase client (hosted only), `accessToken()`, `refreshSession()`, `endSession(reason)`, `requestCode(email)`, `verifyCode(email, code)`, and `useSession()` for components |
| `shared/api/client.ts` | shared | base URL by mode; the local-token middleware (local) or the bearer middleware (hosted) |
| `shared/api/stream.ts` | shared | `readRunEvents(runId, { lastEventId, signal })` and the event-stream parser |
| `shared/api/refusal.ts` | shared | `Refusal` gains `AuthRefusalOut` from the generated types |
| `features/auth/SignInForm.tsx`, `features/auth/SignOutButton.tsx` | features | the two-step form and the button |
| `features/runs/useRunStream.ts` | features | the hook, now on `readRunEvents` |
| `pages/sign-in/SignInPage.tsx` | pages | the page around the form |
| `widgets/header/Header.tsx` | widgets | the sign-out button, hosted only |
| `app/AuthGate.tsx` | app | sign-in page or shell; clears the query cache on a change of user |

The session lives in `shared` because `shared/api/client.ts` needs the
token and shared may not import a feature. The fsd-reviewer's rule that
data access goes through the generated client
(`.claude/agents/fsd-reviewer.md`, check 2) is kept: the stream is a
`GET` on the generated client, and the only other network calls are
`supabase-js`'s own calls to Supabase Auth, which are not the harrier API.

### Signing in

Hosted, with no session, the app renders the sign-in page and nothing
else: no header, no health query, no track query.

1. The page asks for an email address. On submit it calls
   `signInWithOtp({ email, options: { shouldCreateUser: false } })`.
   `shouldCreateUser: false` stops this form from creating accounts;
   accounts are created by invitation (spec 102, open decision 5; spec
   111). Supabase sends a code rather than a link when the project's email
   template carries `{{ .Token }}` (signInWithOtp reference); configuring
   the template is spec 110's.
2. Whatever Supabase answers, except a rate limit or a network failure,
   the page says: "If this address has an account, a sign-in code is on
   its way." The page does not say whether the address has an account.
   A rate limit says: "Too many requests. Wait before asking for another
   code." A network failure says: "Could not reach the sign-in service."
3. The page then asks for the code. On submit it calls
   `verifyOtp({ email, token: code, type: "email" })`. A refusal says:
   "That code is wrong or has expired." Success fires `SIGNED_IN`, and the
   gate renders the shell.

The client is created with `persistSession: true` and
`autoRefreshToken: true` (both the documented defaults) and
`detectSessionInUrl: false`, since a code sign-in puts nothing in the URL
and the app's own query string carries the selected track (spec 094).
The session is kept in the browser's `localStorage` under supabase-js's
key (Open decision 3).

The email address is personal data. It is held in component state only,
never written to a URL, a log, `localStorage` by harrier's code, or an
error message.

### The token on every call

The client's middleware, hosted:

- `onRequest`: reads `accessToken()` (supabase-js `getSession()`, which
  "returns the session, refreshing it if necessary") and sets
  `Authorization: Bearer <token>`. It keeps a clone of the request for
  one retry. With no session it sends the request without the header,
  and the API's 401 `missing` is handled below.
- It never sets `X-Harrier-Token` and never requests `GET /session`.
- The token is never put in a URL, a query string, a log line, or an
  error message the app renders.

Locally the middleware is today's, unchanged: the local token on
state-changing requests and the eight read patterns (`client.ts:31-47`),
fetched from `GET /session` (`client.ts:54-68`).

The base URL is `new URL("/api", location.href)` locally, as today
(`client.ts:10`), and `VITE_HARRIER_API_URL` hosted. Routes are appended
to it the same way in both modes.

### When the API refuses the token

Spec 104's refusals carry `reason` in `AuthRefusalOut`. The middleware's
`onResponse`, hosted, which openapi-fetch lets return a replacement
`Response` (openapi-fetch API reference):

| Response | What the SPA does |
|---|---|
| 401 `expired`, first time for this request | `refreshSession()`; if it yields a token, re-sends the cloned request once with it and returns that response |
| 401 `expired` after the retry, or refresh fails | `endSession("expired")`. When the refresh token is refused, supabase-js removes the session and fires `SIGNED_OUT` itself before `refreshSession` returns, so the notice is set before the refresh is called |
| 401 `missing` or `invalid` | `endSession("invalid")` |
| 403 `anonymous` | `endSession("refused")` |
| 503 `keys_unavailable` | nothing extra; the caller shows the detail as any refusal; the session stays |
| anything else | unchanged |

`endSession(reason)` calls `signOut({ scope: "local" })`, which removes
the session from storage and fires `SIGNED_OUT` (signOut reference). The
gate then shows the sign-in page with one sentence: "Your session ended.
Sign in again." for `expired` and `invalid`, and "This account cannot use
harrier." for `refused`. Concurrent 401s end the session once.

Spec 104 says a 401 "tells the browser to sign the person out". This
spec does that for every 401 except the first `expired`, where a refresh
can still succeed without asking the person anything (Open decision 8).

### The run stream

`readRunEvents(runId, { lastEventId, signal })` calls
`api.GET("/runs/{run_id}/events", { params: { path: { run_id } },
parseAs: "stream", headers, signal })`. openapi-fetch's `parseAs:
"stream"` returns the raw body, and every call accepts fetch options such
as `headers` and `signal` (openapi-fetch API reference). The headers are
`Accept: text/event-stream` and, when `lastEventId` is above zero,
`Last-Event-ID`. Both are allowed by spec 104's CORS list (`Accept` is
CORS-safelisted; `Last-Event-ID` is listed by spec 104). Because it is a
call on the generated client, the bearer middleware applies to it with no
code of its own.

The parser reads the body with a `ReadableStream` reader and follows the
WHATWG event-stream rules: lines end in CRLF, LF or CR; a line starting
with `:` is a comment and is ignored; `id` sets the last event id; `data`
lines are joined with a newline; a blank line dispatches the event. The
server sends only `id` and `data` (`format_sse`,
`services/api/src/harrier_api/runs.py:1335-1337`) and, hosted, a final
comment at token expiry (spec 104, "The event stream"), so `event` and
`retry` are read and ignored.

`useRunStream` keeps its return shape (`useRunStream.ts:128`) and its
behavior on events (`:81-99`). What changes is how it connects:

- **Start.** `begin(run)` aborts any open stream and opens one with
  `lastEventId` 0, as `subscribe` does today (`:77-109`).
- **Clean end, run not terminal.** The server ended the stream (hosted:
  the token expired) before a terminal `state_change` arrived. The hook
  reconnects at once with `Last-Event-ID` set to the last id it received,
  and `accessToken()` supplies a fresh token. If that connection also
  ends clean having delivered no event, the next attempt waits the retry
  delay, so a server that ends streams at once cannot drive a tight loop.
- **Error.** A network failure, a rejected read, or a status other than
  200, 401 and 404: the hook sets `disconnected` and invalidates the
  run's query, as today (`:100-105`), then retries after the retry delay
  with `Last-Event-ID`, until the run is terminal by its query, a new run
  begins, or the component unmounts.
- **404.** The run is gone (spec 104 answers 404 for another owner's run
  too). The hook stops and invalidates the run's query.
- **401.** Handled by the middleware above. If the session ends, the gate
  unmounts the shell and the stream is aborted with it.
- **Terminal `state_change`.** The hook aborts the stream, as it closes
  the `EventSource` today (`:95-97`).
- **Reconnected.** `disconnected` returns to false when a reconnect
  delivers an event. Today it stays true until the next `begin`. This is
  a deliberate change in both modes: `EventSource` reconnected silently,
  and the notice stayed up after the stream had recovered.

The retry delay is 3 seconds. It is a chosen value, a constant with a
comment, not configuration, and not a measured one.

`EventSourceFactory` and every `createEventSource` prop are removed: from
`useRunStream.ts`, from the four components that default it, and from the
six operations sections that pass it on. The four test files that stub it
stub `globalThis.fetch` with a controllable stream instead, as every
other test in `apps/web` already stubs the client. `EventSource` is added
to ESLint's `no-restricted-globals` for `apps/web/src`, so a second
implementation is a lint error (spec 047's failure mode).

Locally, the stream has no `Authorization` header, as today: the local
API does not require a token on the stream route (spec 035;
`services/api/tests/test_api_exposure.py::test_reads_do_not_require_the_token`).

### Signing out and switching users

The header shows "Sign out" in hosted mode only. Pressing it calls
`endSession("signed-out")`, which calls `signOut({ scope: "local" })`
(Open decision 4). Then, in this order, the gate:

1. aborts every open run stream (by unmounting the shell, whose hooks
   abort on cleanup as `useRunStream.ts:64-68` closes today);
2. calls `queryClient.clear()` on the app's client (`App.tsx:27`), so no
   row of the signed-out person stays in memory;
3. renders the sign-in page with no message.

The gate also clears the cache whenever the signed-in user's id differs
from the one the cache was filled under, which covers another tab
signing in as someone else. The cache is keyed by track (spec 094), not
by owner, so without this a second person on the same browser would see
the first person's cached rows until each query refetched.

The access token stays valid at the API until it expires: Supabase says
there is no way to revoke an access token before its expiry (signOut
reference), and spec 104 accepts that window (its Open decision 10).

### Health hosted

The health query is public (spec 104) but runs only inside the shell, so
only after sign-in. When `job_count` is null and no hold is reported, the
badge shows no count instead of "0 jobs" (`HealthBadge.tsx:55`). Locally
the count is a number whenever no hold is reported, so the local badge
does not change.

### Documents

- ADR-004, "Progress transport": an "Amended (spec 114)" note. SSE stays
  the transport and `Last-Event-ID` replay stays the reconnect; the SPA
  reads it with `fetch` and reconnects itself, because `EventSource`
  cannot send the hosted API's bearer header (spec 104, Open decision 4).
- ADR-005, option 1: an "Amended (spec 114)" note. SSE endpoints are read
  through the generated client with `parseAs: "stream"`, not with native
  `EventSource`. Neither ADR's decision changes.
- `docs/architecture.md`, hosted section: the SPA's hosted mode, its four
  variables, the sign-in method and this spec.

Spec 006's Web section is a shipped spec's record of what it built and is
not edited; ADR-004's note is where a reader finds the change.

## What spec 119 inherits

Spec 104's Open decision 6 stops serving the capture GET and form post
hosted, and says a later spec gives the SPA a capture page the bookmarklet
navigates to. That spec is 119, "The browser captures a job on the hosted
service", a stub created with this refinement. This spec does not build it
(Open decision 1). What the reading for this spec found, for spec 119 to
start from:

- The bookmarklets navigate with `window.open` to
  `http://localhost:'+PORT+'/capture/add?` plus the fields as a query
  string, the description cut to 8000 characters (`docs/bookmarklets.md:49`
  and the three single-site variants after it). A hosted variant needs the
  SPA's origin in place of `localhost:PORT`.
- Fields in a query string reach Vercel's request logs. Fields in the
  fragment (`#...`) are never sent to the server. Job data is not
  personal data under the privacy rule, but a fragment keeps the SPA's
  host from logging what each person captures.
- The app has no router; it selects views in `App.tsx` and keeps the track
  in the query string (spec 094). A fragment route needs no rewrite rule
  on Vercel.
- If the person is not signed in, the captured fields must survive the
  sign-in page. With the code sign-in of this spec there is no redirect,
  so component state holds them; a link sign-in would need
  `sessionStorage`.
- The page posts `POST /capture/add` through the generated client, so
  this spec's bearer middleware applies. Capture writes to the default
  track and takes no `track` parameter
  (`services/api/src/harrier_api/deps.py:51-55`).

## Failure modes

- **`VITE_HARRIER_AUTH` has another value.** `pnpm build` and `pnpm dev`
  fail: `VITE_HARRIER_AUTH must be empty or supabase`.
- **A hosted variable missing or malformed.** No `VITE_SUPABASE_URL`, one
  not `https://` or carrying a path; no `VITE_HARRIER_API_URL`, or one not
  `https://` or carrying a path, query or fragment; no key: the build
  fails naming the variable.
- **A secret key in the build.** `VITE_SUPABASE_PUBLISHABLE_KEY` begins
  `sb_secret_`, or is a JWT whose `role` is `service_role`: the build
  fails naming the variable, never printing the value, in local mode too.
- **A bundle built with a bad configuration anyway.** The start-up check
  throws; the app renders "This build of harrier is misconfigured:" and
  the variable's name, and sends no request.
- **Supabase unreachable at sign-in.** "Could not reach the sign-in
  service." No retry loop; the person submits again.
- **Address without an account.** The neutral sentence; no account is
  created.
- **Too many code requests.** The rate-limit sentence. Supabase's default
  is one magic link request per 60 seconds (passwordless guide); whether
  codes share that limit is checked in spec 110's staging check.
- **Wrong or expired code.** "That code is wrong or has expired." The
  person can ask for a new code.
- **Access token expired between refreshes.** A tab asleep past the
  token's lifetime wakes; `getSession()` refreshes before the next call.
  If a call still gets 401 `expired`, one refresh and one retry; then
  sign-in.
- **Refresh token revoked or reused.** Supabase refresh tokens are single
  use, and reuse outside a short window revokes the session (sessions
  guide). The refresh fails, and the session ends with "Your session
  ended. Sign in again."
- **Forged, foreign or missing token.** 401 `invalid` or `missing`: the
  session ends at once, no retry.
- **Anonymous session.** 403 `anonymous`: the session ends with "This
  account cannot use harrier."
- **Keys unavailable at the API.** 503 `keys_unavailable`: the refusal's
  detail is shown where the call was made; the person stays signed in.
- **Stream ended at token expiry.** Reconnect with `Last-Event-ID` and a
  fresh token; no event is lost or shown twice.
- **Stream ends at once, repeatedly.** After one empty clean end, each
  reconnect waits the retry delay.
- **Network drops mid-stream.** `disconnected` shows, the run's state is
  refetched, and the hook retries every retry delay until the run is
  terminal or the view goes away.
- **Another owner's run, or a run the API forgot.** 404: the hook stops.
- **Starting a run hosted.** Spec 104 answers 501 until spec 106. The
  existing refusal text shows the API's detail; this spec hides no
  control.
- **Sign-out while a stream is open.** The stream is aborted before the
  cache is cleared; no event from the old session is applied after.
- **Two tabs, one signs out.** supabase-js removes the stored session;
  whether the other tab sees `SIGNED_OUT` at once is an assumption
  (Proof / origin). If it does not, its next call answers 401 after the
  token expires, or sooner if the server-side session matters to the
  API, and the session ends there.
- **Another person signs in on the same browser.** The user id changes,
  and the cache is cleared before the shell renders.
- **Hosted vars present in a local build.** `VITE_HARRIER_AUTH` unset:
  local mode; the others are ignored.

## Acceptance criteria

Each criterion cites the test that proves it. A web test file is named by its
file name under `apps/web/src`. The hosted tests are in their own files,
because a bundle's mode is fixed before any module is read, so a hosted test
file sets the build's variables first (Amendment of 2026-10-14, item 9).

Local mode:

- [x] With no `VITE_` variable set, every existing web test passes, after
      the five test files that stubbed `EventSource` are moved to a fetch
      stream stub with their test names unchanged, including, in
      `RunPanel.test.tsx`: "start subscribes to events and renders streamed
      lines until terminal", "a broken stream refetches server truth
      instead of staying active" and "a dropped stream says so instead of
      looking stuck"; and in `OperationsPage.test.tsx`: "every operations
      write carries the token and no read does". One test changed its
      expectation: "a backups list refused for its token says so" in
      `SettingsPage.test.tsx` now sees the API's own sentence for a refused
      local token (Amendment of 2026-10-14, item 2).
- [x] Local mode never imports or creates a Supabase client and never
      sends `Authorization`: "local mode never creates a Supabase client or
      sends a bearer token" in `App.test.tsx`, which also sees that the
      package's mock factory, which runs on import, never ran.
- [x] `just demo` serves the built SPA from the API, as before, and the
      local stream route is called at `/api/runs/{id}/events`: checked by
      hand (Amendment of 2026-10-14, item 13), and the path is asserted by
      "start subscribes to events and renders streamed lines until
      terminal" in `RunPanel.test.tsx`.

Configuration:

- [x] `readAppMode` refuses each case under Failure modes from
      "`VITE_HARRIER_AUTH` has another value" to "A secret key in the
      build" with its text, and no message contains a key value: "a hosted
      build refuses each bad variable" in `mode.test.ts`, one case each.
      "local mode reads nothing but the switch" in `mode.test.ts` proves
      that hosted variables in a local build are ignored, and "a secret key
      is refused in a local build too" in `mode.test.ts` that a secret key
      is not. No unrelated `VITE_` variable reaches the bundle, checked by
      hand with real builds (Amendment of 2026-10-14, item 11).
- [x] `pnpm build` with `VITE_HARRIER_AUTH=supabase` and no other hosted
      variable exits non-zero naming `VITE_SUPABASE_URL`: "a hosted build
      with no other hosted variable stops before it bundles" in
      `viteConfig.test.ts`, and the command itself, run by hand.
- [x] A bundle whose start-up check throws renders the misconfiguration
      page and makes no request: "a misconfigured build says so and calls
      nothing" in `App.misconfigured.test.tsx`.

Sign-in and session:

- [x] Hosted with no session, only the sign-in page renders and no API
      request is made: "hosted without a session shows only sign-in" in
      `App.hosted.test.tsx`.
- [x] Requesting a code calls `signInWithOtp` with
      `shouldCreateUser: false`; an unknown address and a known one show
      the same sentence; a rate limit shows its own: `SignInForm.test.tsx`:
      "the code request never says whether an account exists" and "a rate
      limit and a network failure each say so". The sentences are written
      out in the test, not imported, and a code check that gets no answer
      says so: "a code check that gets no answer says so" in
      `SignInForm.test.tsx`. supabase-js returns a request that got no answer
      as an error with status 0, and the fake does the same.
- [x] A wrong code shows the refusal sentence: "a wrong code shows the
      refusal sentence" in `SignInForm.test.tsx`. A right one renders the
      shell: "a verified code opens the app" in `App.hosted.test.tsx`.
- [x] The email address never appears in a request URL to the harrier
      API, in `localStorage` written by harrier's code, or in a console
      call during the sign-in tests: "the address reaches Supabase and
      nothing else" in `SignInForm.test.tsx`.
- [x] The Supabase client is made from the project's URL and key with
      `persistSession` and `autoRefreshToken` on and `detectSessionInUrl`
      off: "the Supabase client is made from the project's URL and key" in
      `client.test.ts`.

The token:

- [x] Hosted, walking the tracker, a tracker write, an operations write
      and a run stream, every request to the API carries
      `Authorization: Bearer <token>`, none carries `X-Harrier-Token`,
      `GET /session` is never requested, and no request URL contains the
      token: "hosted calls carry the bearer token and nothing else" in
      `client.test.ts`. The whole app does the same for what it loads and
      one run with its stream: "the hosted app sends the bearer token on
      every request it makes" in `App.hosted.test.tsx`. The tracker and
      operations writes are covered by the first test only.
- [x] Hosted requests go to `VITE_HARRIER_API_URL`, not the page origin
      (the same two tests).
- [x] A 401 `expired` refreshes once and retries once, and the retried
      request carries the new token; a second 401 ends the session: "an
      expired token is refreshed once and retried" in `client.test.ts`.
      Concurrent refusals share the one refresh: "concurrent expired
      refusals share one refresh" in `client.test.ts`. A refresh the
      server refuses ends the session with no retry, and the page says why:
      `client.test.ts`: "a refresh the server refuses ends the session
      without a retry" and "a refresh that gets no answer ends the session
      without a retry"; and "a refresh token the server refuses ends the
      session with its sentence" in `App.hosted.test.tsx`. The retry is a
      copy made before the first attempt, so a write carries its body
      again: "a retried write carries its body again" in `client.test.ts`.
      A retry that gets no answer says so: "a retry that gets no answer says
      so" in `client.test.ts`.
- [x] A 401 `invalid`, a 401 `missing` and a 403 `anonymous` each end the
      session with no retry and show their sentence: "a refused token ends
      the session" in `App.hosted.test.tsx`, one case each, and "a refused
      token is never retried" in `client.test.ts`.
- [x] A 503 `keys_unavailable` leaves the session in place and shows the
      API's detail: "unavailable keys never sign anyone out" in
      `client.test.ts` and in `App.hosted.test.tsx`, and "a read refused for
      unavailable keys shows the API's sentence" in `App.refusals.test.tsx`
      for the read-only fetchers.
- [x] Three concurrent 401 `invalid` responses call `signOut` once: "three
      concurrent refusals sign the person out once" in `client.test.ts`.
- [x] `Refusal` in `shared/api/refusal.ts` includes
      `components["schemas"]["AuthRefusalOut"]`, and the reasons the
      middleware branches on are typed from that schema, not written by
      hand (`ENDS_SESSION` in `shared/api/client.ts` is a `Record` keyed by
      the generated reasons). The union is pinned by type: "an outreach
      refusal is read through the contract's types" in
      `OutreachPage.test.tsx` and "a tracker refusal is read through the
      contract's types" in `TrackerPage.test.tsx`.
- [x] The contract declares the hosted 403 (Amendment of 2026-10-14, item
      2): `test_the_contract_declares_the_anonymous_refusal` in
      `services/api/tests/test_hosted_auth.py`, and `just contract` leaves
      no diff.
- [x] A request that gets no answer, which is what the browser sees of an
      unhandled hosted 500, says so, and an aborted request is left alone:
      "a request that gets no answer says so" in `client.test.ts`. A page
      shows the sentence, and the person stays signed in: "a request that
      gets no answer shows the sentence for it" in `App.hosted.test.tsx`.

The stream:

- [x] `grep -rn EventSource apps/web/src` finds nothing, and
      `no-restricted-globals` names `EventSource` in
      `apps/web/eslint.config.js`. A line using it fails `pnpm lint`, and
      the same line passes with the rule removed (run by hand).
- [x] The parser handles CRLF, LF and CR line ends, comments, multi-line
      `data`, and a chunk boundary inside a line or inside a multi-byte
      character: `stream.test.ts`: "the event-stream parser follows the line
      rules" and "a chunk boundary inside a multi-byte character loses
      nothing".
- [x] A stream that ends clean before a terminal state reconnects with
      `Last-Event-ID` equal to the last id received, and the log shows each
      event once: "a stream ended at token expiry resumes where it
      stopped" in `RunPanel.test.tsx`. The reconnect carries the token as
      it is then: "a reconnected stream carries the token as it is then" in
      `client.test.ts`.
- [x] Two consecutive empty clean ends do not reconnect faster than the
      retry delay: "a stream that keeps ending waits before reconnecting"
      in `RunPanel.test.tsx`, which holds the retry timer rather than
      faking the clock. An end after events reconnects at once: "a stream
      that delivered something is reconnected at once" in
      `RunPanel.test.tsx`. A stream that failed is not reopened once the run
      is over, and an event that is not JSON is skipped: `RunPanel.test.tsx`:
      "a stream that failed is not reopened once the run is over" and "an
      event that is not JSON is skipped, and the stream carries on".
- [x] After a network error the hook shows `disconnected`, refetches the
      run, retries after the delay, and clears `disconnected` on the first
      event of the new connection: "a recovered stream stops saying it is
      disconnected" in `RunPanel.test.tsx`.
- [x] A 404 on the stream stops reconnecting: "a run the stream does not
      know stops the reconnecting" in `RunPanel.test.tsx`.
- [x] Unmounting, a new `begin`, and a terminal `state_change` each abort
      the open request: "a stream is aborted when it is no longer watched"
      in `useRunStream.test.tsx`.

Sign-out and users:

- [x] Hosted, the header shows "Sign out" and local does not: "a verified
      code opens the app" in `App.hosted.test.tsx` and "local mode never
      creates a Supabase client or sends a bearer token" in `App.test.tsx`.
- [x] Sign-out calls `signOut({ scope: "local" })`, aborts open streams,
      empties the query cache, in that order, and shows the sign-in page:
      "sign-out forgets the person's data" in `App.hosted.test.tsx`.
- [x] A `SIGNED_IN` for a different user id clears the cache before the
      shell renders: "a different user never sees the previous user's
      cache" in `App.hosted.test.tsx`, which runs again under StrictMode,
      as production does: "a different user never sees the previous user's
      cache under StrictMode" in `App.hosted.test.tsx`.

Health:

- [x] A health body with `job_count: null` and no hold renders no count:
      "no job count is shown when the API reports none" in
      `HealthBadge.test.tsx`.

Layers and documents:

- [x] `pnpm lint` passes with the boundaries rule unchanged; no file under
      `shared/` imports a feature, and the sign-in form and button live in
      `features/auth`.
- [x] ADR-004 and ADR-005 carry the "Amended (spec 114)" notes under
      Behavior, "Documents", and no other line of either changes.
- [x] `docs/architecture.md`'s hosted section names `VITE_HARRIER_AUTH`
      and the three hosted variables and cites this spec. `.env.example`
      names the four variables in comments (Amendment of 2026-10-14, item
      8).

Gates:

- [x] `pnpm type-check`, `pnpm lint` and `pnpm test` pass in
      `apps/web`.
- [x] `just contract` leaves no diff.
- [x] `just check` passes, run as its recipes. `check-python`: ruff,
      format, pyright, lint-imports and the whole of pytest against the test
      Postgres, 3819 passed and 29 skipped on the second run. The first run
      had five failures, all
      `test_a_failed_migration_leaves_no_trace_of_itself`, whose catalog
      fingerprint includes cluster-wide role memberships, and the
      difference was other sessions' `harrier_t_*` roles on the shared
      server; the test passes alone. `check-ts`, `contract`, `aie-check` and
      `spec-structure` were run on their own and pass.

## Honest limitations

- The session, refresh token included, sits in `localStorage`. Any script
  running on the SPA's origin can read it, so a cross-site scripting bug
  is a stolen session. React escapes rendered text and the SPA loads no
  third-party script, but this spec adds no Content Security Policy; the
  response headers Vercel sends are spec 110's.
- The session `supabase-js` stores holds the signed-in user object, which
  carries the person's email address. harrier's own code never writes the
  address anywhere, but the library's `localStorage` entry does, readable by
  the same scripts and until sign-out. The tests fake the client, so they
  cannot show this either way (privacy review of PR #328).
- Sign-out does not stop the access token working at the API until it
  expires, one hour by default (sessions guide, spec 104).
- The neutral sign-in sentence hides account existence from the page, not
  from the network: if Supabase answers an unknown address differently,
  the response shows it in the browser's developer tools.
- Cross-tab behavior (one refresh shared by several tabs, sign-out seen
  by every tab) is supabase-js's, not harrier's, and is not confirmed
  from its documentation.
- Hosted runs answer 501 until spec 106, so the hosted reconnect path is
  exercised by tests, not by a real run, until then.
- jsdom and Node's `fetch` stand in for browsers in the tests. A browser
  that buffers a streamed response would deliver events late; spec 110's
  staging check reads a real stream through Fly's proxy.
- The 3 second retry delay is chosen, not measured.
- The key check knows two shapes, `sb_secret_` and a `service_role` JWT.
  A future secret key format would pass it.
- Vite also reads `.env` files in `apps/web`. A developer's
  `apps/web/.env.local` can switch a local build to hosted mode. It is
  gitignored (`.gitignore:2-3`), but the classification table matches
  whole repository-relative paths with `fnmatch`
  (`services/api/tests/test_classification_coverage.py:32-35`), so its
  `.env.local` entry does not cover `apps/web/.env.local`. That gap is
  the one spec 110 already records for env files.
- Sign-in by code needs the project's email template and sender
  configured. Supabase's built-in sender is not assumed to reach invited
  users; spec 110 configures it. Spec 111 creates invited users, who sign
  in here with the same code; there is no invitation page (decision D6,
  2026-10-11).
- The line numbers cited here are refreshed by the change that implements
  this spec, not before (decision D9, 2026-10-11).

## Migration

None for the local product: no variable it reads changes, and no file
moves. The local SPA's only visible changes are the `disconnected` notice
clearing after a recovered stream and the badge showing no count when
none is reported. No hosted SPA exists. When one does, spec 110 sets the
four variables per Vercel environment, staging and production each with
their own Supabase project and API URL (spec 102, open decision 4).

## Changes to approved specs

Each line below is a change this spec asks of an approved spec. None is
written into that spec here; each is an amendment Akin approves with this
spec.

- **Spec 104:** its Out of scope gives "a hosted capture page" to spec 114
  (its Open decision 8). It now reads spec 119.

## Options weighed

- **Keep `EventSource` and put the token in the query string.** Spec 104
  refuses tokens in URLs because URLs reach access logs at every proxy.
  Not chosen there, so not available here.
- **A third-party fetch-based SSE client.** It would bring its own
  `fetch` call, outside the generated client, so the bearer middleware
  would not apply and the fsd-reviewer's data-access rule would be
  broken. The server's format is two fields. Not chosen: a parser of our
  own on the generated client's stream.
- **Infer hosted mode from `VITE_SUPABASE_URL`.** One variable fewer.
  Spec 104 made the API's mode explicit for the same reason it applies
  here: a stray variable should not change who the app thinks it is
  talking to. Not chosen.
- **Runtime configuration, a `config.json` fetched at load.** One bundle
  for both environments. It adds a request before anything renders and a
  file Vercel must serve per environment, while Vercel already builds per
  environment with its own variables. Not chosen.
- **Proxy the API through Vercel rewrites to one origin.** No CORS, and a
  cookie would become first-party. It puts a second proxy in front of a
  long-lived stream, with timeouts this spec has not checked, and spec
  104 already designed for two origins. Not chosen.
- **Sign the person out on every 401.** Simpler, and literally what spec
  104's sentence says. A token that expired between the last refresh and
  the request would then cost a sign-in that a refresh avoids. Not
  chosen (Open decision 8).
- **One spec with the capture page.** See Open decision 1.

## Decisions taken (2026-10-11)

Akin took these in chat on 2026-10-11 ("go with your recommendations").

- **D5, two settings forms.** This spec gains a credentials page, asked
  by specs 106 and 116, and a Telegram settings form, asked by spec 109.
  Written as the Amendment (2026-10-11) below.
- **D6, invitations sign in here.** An invited user signs in with the same
  emailed one-time code as every other user (Behavior, "Signing in").
  There is no separate invitation page; spec 111 creates the user and
  prints no link.
- **D7, the Telegram chat id is secret everywhere.** The Telegram form
  treats it as it treats the bot token (Amendment below).
- **D9, line numbers.** Refreshed by this spec's implementing change
  (Honest limitations).

None of Open decisions 1 to 9 below was the subject of these decisions;
they stand as written.

## Open decisions for Akin

1. **The capture page.** Recommended: split it into spec 119, a stub
   created with this refinement, starting from "What spec 119 inherits".
   This spec already rewrites the stream in eleven source files and four
   test files and adds sign-in, the token path and sign-out; the capture
   page adds a page, a fragment route in a router-less app, and new
   bookmarklet text, which `change-boundary` asks to review apart. Alternative: build it here, as the stub's headline said, in one
   larger change.
2. **Sign-in method.** Recommended: an emailed one-time code. No
   redirect, so no redirect allow list per environment and nothing in
   the URL beside the track; it works when the email is read on another
   device. Alternative: a magic link, one click instead of typing a
   code, but under PKCE the code exchange must happen in the browser
   that asked (PKCE flow guide), and the app must read a `code` parameter
   beside its own `track`. A second alternative, email and password,
   makes spec 111 build password setting and reset.
3. **Where the session is kept.** Recommended: supabase-js's default,
   `localStorage`, so a reload or a new tab stays signed in. Alternative:
   `sessionStorage` or memory only, which narrows what a script can read
   after the tab closes and makes every new tab sign in again.
4. **Sign-out scope.** Recommended: `local`, this browser's session only;
   Supabase's reference marks it "recommended for most apps". The
   default, `global`, signs the person out on every device. A "sign out
   everywhere" control can come later.
5. **How the SPA knows it is hosted.** Recommended: `VITE_HARRIER_AUTH=
   supabase`, explicit, mirroring spec 104's `HARRIER_AUTH`. Alternative:
   hosted whenever `VITE_SUPABASE_URL` is set.
6. **The stream reader.** Recommended: the generated client with
   `parseAs: "stream"` and a parser in `shared/api/stream.ts`.
   Alternative: a fetch-based SSE library (Options weighed).
7. **Loading supabase-js.** Recommended: a dynamic import on first use in
   hosted mode, so the local bundle neither loads nor initializes it.
   Alternative: a static import, simpler, with the library in every
   bundle but unused locally.
8. **The first 401 `expired`.** Recommended: refresh and retry once, then
   sign-in. Alternative: sign-in on every 401, as spec 104's sentence
   reads.
9. **The key's variable name.** Recommended:
   `VITE_SUPABASE_PUBLISHABLE_KEY`, accepting a publishable key or the
   legacy anon key. Supabase calls the anon key the legacy form of the
   publishable key and says both work side by side (API keys guide).
   Alternative: `VITE_SUPABASE_ANON_KEY`, ADR-013 decision 7's word.

## Proof / origin

- `specs/104-the-hosted-api-authenticates-every-request.md`: Scope items
  8 and 10, "Refusals" (the `AuthRefusalOut` reasons and why 503 is not
  401), "What the hosted API serves", "The event stream", "Cross-origin
  requests" (allowed headers), and Open decisions 4, 6, 8 and 10.
- `docs/adr/ADR-013-hosted-multi-tenant-deployment.md`, decision 7: what
  Vercel holds.
- `docs/adr/ADR-005-api-contract-seam.md`: one generated client; lines
  33-34 on `EventSource`, amended here.
- `docs/adr/ADR-004-long-running-work.md`, "Progress transport", lines
  54-55, amended here; `specs/006-run-manager-and-sse.md:77-78`.
- `docs/adr/ADR-001-frontend-framework.md` and
  `.claude/agents/fsd-reviewer.md`: the layers and the data-access rule.
- `specs/035-local-api-exposure.md`: the local token this spec leaves
  alone. `specs/047-apply-page.md`: one stream implementation.
  `specs/094-the-browser-works-in-a-chosen-search-track.md`: the track in
  the query string and track-keyed cache.
- Code read: `apps/web/src/shared/api/client.ts`,
  `apps/web/src/shared/api/refusal.ts`,
  `apps/web/src/features/runs/useRunStream.ts`,
  `apps/web/src/features/runs/RunPanel.tsx`,
  `apps/web/src/features/health/HealthBadge.tsx`,
  `apps/web/src/app/App.tsx`, `apps/web/src/widgets/header/Header.tsx`,
  `apps/web/vite.config.ts`, `apps/web/eslint.config.js`,
  `services/api/src/harrier_api/app.py` (`get_session` at 355,
  `stream_run_events` at 1235-1252, `ApiPrefixMiddleware` at 1441),
  `services/api/src/harrier_api/runs.py` (`stream` at 1201,
  `format_sse` at 1335-1337), `services/api/src/harrier_api/deps.py:51-55`,
  `docs/bookmarklets.md:49`.
- Counts: `grep -rn "new EventSource" apps/web/src` (four, outside
  tests); `grep -ln "useRunStream(" -r apps/web/src | grep -v test`
  (eleven files, ten callers); `grep -ln FakeEventSource -r apps/web/src`
  (four test files); `grep -rn "import.meta.env" apps/web/src` (none).

Vendor documentation, each read 2026-10-10:

- supabase-js `createClient` auth options: `autoRefreshToken`,
  `persistSession` and `detectSessionInUrl` default to true; `flowType`
  defaults to implicit. https://jsr.io/@supabase/supabase-js@2.107.0/doc/~/SupabaseClientOptions.auth
- supabase-js reference, initializing: `persistSession` saves the session
  to local storage. https://supabase.com/docs/reference/javascript/initializing
- supabase-js `getSession`: returns the session, refreshing it if
  necessary; may be null. https://supabase.com/docs/reference/javascript/auth-getsession
- supabase-js `signOut`: scopes `global` (default), `local` ("recommended
  for most apps") and `others`; removes the session from local storage
  and fires `SIGNED_OUT`; an access token cannot be revoked before it
  expires. https://supabase.com/docs/reference/javascript/auth-signout
- supabase-js `signInWithOtp`: a code is sent when the template carries
  `{{ .Token }}`, a link when it carries `{{ .ConfirmationURL }}`.
  https://supabase.com/docs/reference/javascript/auth-signinwithotp
- supabase-js `verifyOtp`: verifies an emailed code with `type: "email"`.
  https://supabase.com/docs/reference/javascript/auth-verifyotp
- supabase-js `onAuthStateChange`: events `INITIAL_SESSION`, `SIGNED_IN`,
  `SIGNED_OUT`, `TOKEN_REFRESHED`, `USER_UPDATED`, `PASSWORD_RECOVERY`.
  https://supabase.com/docs/reference/javascript/auth-onauthstatechange
- Supabase, passwordless email: `shouldCreateUser: false` stops automatic
  sign-up; by default one request per 60 seconds and links expire after
  one hour; redirect URLs are an allow list.
  https://supabase.com/docs/guides/auth/auth-email-passwordless
- Supabase, PKCE flow: the code verifier is stored where the flow began;
  the exchange must run in the same browser and device; the code is
  valid for 5 minutes. https://supabase.com/docs/guides/auth/sessions/pkce-flow
- Supabase, sessions: default access token lifetime one hour; refresh
  tokens are single use, with a short reuse window; signed-out sessions
  are removed. https://supabase.com/docs/guides/auth/sessions
- Supabase, API keys: the publishable key is safe to expose in a web
  page; the anon key is its legacy form and both work together; secret
  keys never go in a browser, and Supabase answers 401 to one used from a
  browser. https://supabase.com/docs/guides/api/api-keys
- openapi-fetch API: `parseAs: "stream"` returns the raw body; any fetch
  option (`headers`, `signal`) may be passed per call; `onResponse` may
  return a replacement `Response`. https://openapi-ts.dev/openapi-fetch/api
- Vite, env and modes: only `VITE_` variables reach client code; existing
  environment variables override `.env` files; `import.meta.env` is
  replaced statically at build. https://vite.dev/guide/env-and-mode
- MDN, using readable streams: `response.body` as a stream, `getReader`,
  `read`, and aborting a fetch with an `AbortSignal`.
  https://developer.mozilla.org/en-US/docs/Web/API/Streams_API/Using_readable_streams
- WHATWG HTML, server-sent events: line endings, comments, `id`, `data`,
  dispatch on a blank line, and `Last-Event-ID` on reconnect.
  https://html.spec.whatwg.org/multipage/server-sent-events.html
- MDN, `EventSource()`: the only option is `withCredentials`; no request
  headers (as cited by spec 104).
  https://developer.mozilla.org/en-US/docs/Web/API/EventSource/EventSource

Assumptions, each checked in spec 110's staging check or during
implementation:

- With `shouldCreateUser: false`, Supabase answers an unknown address
  with an error the SPA can tell apart from a rate limit and a network
  failure. The neutral sentence does not depend on which.
- Codes and links share the 60 second request limit.
- `getSession()` refreshes a token that is about to expire, not only one
  that has expired. If it does not, the first call after expiry takes the
  401 `expired` path, which is why that path exists.
- supabase-js shares one session across tabs of the same origin and lets
  each tab see another's sign-out. Not found in the reference pages read.
- `signOut({ scope: "local" })` clears the stored session even when the
  server call fails, for example with an already invalid token.
- A streamed response from Fly reaches the browser unbuffered. The route
  sets `X-Accel-Buffering: no` (`app.py:1251`); Fly's proxy is
  checked on staging.
- Supabase's built-in email sender can deliver codes to invited users, or
  a custom sender is needed. Spec 110 decides.

## Out of scope

- Server-side verification, CORS and the 401 contract: spec 104.
- The hosted capture page and hosted bookmarklets: spec 119 (Open
  decision 1).
- Sign-up, invitations, export and deletion: spec 111.
- Vercel project, environment variables per environment, email template,
  redirect URLs, response headers and Content Security Policy: spec 110.
- Hiding or explaining run controls while hosted runs answer 501: spec
  106 lifts the 501, and until then the API's detail is shown.
- Multi-factor sign-in, OAuth providers, a "sign out everywhere" control,
  and an account page.
- Any change to the local token, `GET /session` or the local capture
  routes (spec 035).

## Amendment (2026-10-11): the credentials page and the Telegram settings form

Akin took decision D5 on 2026-10-11. Specs 106 and 116 asked for a
credentials page for spec 116's routes; spec 109 asked for a settings form
for its Telegram routes. Both are hosted only, both are sections of the
existing settings page (`apps/web/src/pages/settings/SettingsPage.tsx`),
and both call only the generated client. They are built after the routes
they call exist: spec 116's `/credentials` routes and spec 109's
`/notifications/telegram` routes. This amendment changes no contract; its
types come from the contract those specs regenerate.

### Where they live

| Path | Layer | Holds |
|---|---|---|
| `features/credentials/CredentialForm.tsx` | features | the form for one kind: its fields, save and remove |
| `features/notifications/TelegramForm.tsx` | features | the Telegram form: save and clear |
| `pages/settings/CredentialsSection.tsx` | pages | the kinds and their state, one form per kind |
| `pages/settings/TelegramSection.tsx` | pages | the Telegram state and its form |

### The credentials page

- **State.** From `GET /credentials`: one row per kind, set or not set,
  and `updated_at`. The `telegram` row shows its state and points to the
  Telegram form; it has no fields here, because spec 116 answers 404 to
  `PUT /credentials/telegram`.
- **Fields.** `ai`: the provider (`openai-api` or `anthropic-api`), the
  key and an optional model. `apify`: the token. `hunter`: the key. Every
  secret field is a password input with autocomplete off.
- **Save.** `PUT /credentials/{kind}`. On 200 the form clears every field
  and shows the new state. A 422 shows the field and the problem the body
  names. Nothing is shown back: spec 116 returns no value, and the page
  offers no reveal.
- **Remove.** `DELETE /credentials/{kind}`, after a confirmation in the
  page.

### The Telegram form

- **State.** From `GET /notifications/telegram`: configured or not, and
  `updated_at`.
- **Fields.** The bot token and the chat id. Both are secrets (decision
  D7): password inputs with autocomplete off, cleared after any answer.
- **Save.** `PUT /notifications/telegram`. A refusal shows the `detail`
  of spec 109's `TelegramRefusalOut` for its `reason`. The form says,
  before the fields, to send the bot one message first, which is what spec
  109's `chat_unreachable` needs.
- **Clear.** `DELETE /notifications/telegram`.

On both forms, a submitted value never appears in rendered text, a URL,
`localStorage` written by harrier's code, or a console call.

### Local mode

Neither section renders, and no request to either route family is made.
Locally credentials and Telegram settings live in `.env`, and both route
families answer 404 (specs 116 and 109).

### Acceptance criteria (amendment of 2026-10-11)

- [x] Hosted, the settings page shows each kind's state from `GET
      /credentials` and no value: "the credentials page shows each kind's
      state and never a value" in `SettingsPage.hosted.test.tsx`.
- [x] Saving a credential sends `PUT /credentials/{kind}` with the form's
      fields, clears every field on 200, shows the field and problem of a
      422, and leaves no submitted value in rendered text, a request URL,
      `localStorage` or a console call: `CredentialForm.test.tsx`: "a saved
      credential leaves no value in the page" and "a refused credential
      shows its field and problem, never its value". Each kind sends its
      own fields, and removing asks first: `CredentialForm.test.tsx`: "each
      kind sends its own fields" and "removing a credential asks first,
      then deletes it". A request that gets no answer says so and leaves
      the form usable: `CredentialForm.test.tsx`: "a save or a removal that
      gets no answer says so and leaves the form usable".
- [x] The Telegram form sends `PUT /notifications/telegram`, shows the
      `detail` for each refusal reason, clears both fields after any
      answer, and neither the bot token nor the chat id appears in
      rendered text, a request URL, `localStorage` or a console call: "the
      Telegram form treats the chat id as a secret" in
      `TelegramForm.test.tsx`. It shows `TelegramRefusalOut.detail` on a 400
      or 502 and the `field` and `problem` of `CredentialRefusalOut` on a
      422, and `refusalMessage` does not throw on either: "the Telegram form
      reads the refusals these routes send" in `TelegramForm.test.tsx` and
      "a credential refusal reads as its field and problem" in
      `refusal.test.ts`. A save that gets no answer says so, clears the
      secrets and leaves the form usable: "a save that gets no answer says
      so, clears the secrets and stays usable" in `TelegramForm.test.tsx`.
- [x] Locally neither section renders and no request goes to
      `/credentials` or `/notifications/telegram`: "local settings show no
      credentials or Telegram section" in `SettingsPage.test.tsx`.
- [x] The forms read their types from `components["schemas"]` in the
      generated contract, not from hand-written types, and `pnpm lint`
      passes with the boundaries rule unchanged.

## Amendment (2026-10-14): what building it settled

Written in the change that builds this spec, whole, as Akin decided on
2026-10-13. The text above stands except where this section says otherwise.
The counts and line numbers in the Problem were read from the tree at
`f10e944`, the commit this change started from, and the server-side line
numbers are refreshed to it (decision D9).

### What the build found

1. **The counts were stale.** At `f10e944` there are five `new EventSource`
   outside tests (`SettingsPage.tsx` is the fifth, through the backups
   section), twelve files that call `useRunStream(` (eleven callers and the
   hook's own module), five test files with a `FakeEventSource`, and one
   `createEventSource` prop threaded through the operations sections. All
   are moved. The criteria above carry the new numbers.
2. **The contract changes, which "No contract change" had wrong.** Spec 104
   recorded that the 403 for an anonymous session is in no contract and that
   spec 114 declares it when it rewrites the web call sites it breaks.
   Every route that needs a token now declares a 403 body: `AuthRefusalOut`,
   or, where the route also takes spec 035's local token, `ErrorOut` or
   `AuthRefusalOut`, since the local refusal is an `HTTPException` whose body
   is `{"detail": ...}` (`declare_403` in `services/api/src/harrier_api/deps.py`,
   called by `DatabaseRoute`; the contract regenerated by `just contract`;
   `test_the_contract_declares_the_anonymous_refusal` proves the declaration
   against what the hosted app answers). The generated `data` is then never
   undefined once `error` is handled, so the guards that read an absent body
   as "the local API token was not accepted" fail `pnpm lint` (31 errors,
   counted from its report, in 23 files) and are removed, and the three
   constants that held that sentence go with them. Two consequences are recorded here, not hidden. A refused local
   token now reads in the API's own sentence ("this request did not present
   the local API token. The UI sends it automatically; a page that is not the
   harrier UI cannot."), which is a visible change in local mode, and one
   test expects it. And six read-only fetchers that discarded `error`
   (the backups list, the schedule, the command list, the container facts,
   the track kinds and the inbox archive) now read it, so
   a 503 `keys_unavailable` shows its detail where the call was made, as the
   criterion asks.
3. **Routes hosted never serves.** `GET /session`, `GET /capture/add` and
   `POST /capture/add-form` stay in the one document, undeclared as 404
   hosted, as spec 104 records. The SPA never requests `/session` hosted:
   only the local middleware does, and "hosted calls carry the bearer token
   and nothing else" in `client.test.ts` asserts it. The other two have no caller in
   `apps/web/src`. Declaring a 404 on them would make the document differ by
   mode, which ADR-005 forbids, so nothing is declared.
4. **A hosted 500 outside CORS.** Spec 134 answers an exhausted pool with
   Starlette's own plain 500, sent outside the CORS layer, so the browser
   sees a rejected `fetch`, not a status. The hosted client's `onError`
   turns that rejection into one sentence, "The harrier API did not answer.
   It may be down, or it may have failed on this request.", and leaves an
   `AbortError` alone. The sentence says both causes because the browser
   cannot tell them apart.
5. **Spec 132's 501 and `: token expired`.** The 501 body is `ErrorOut`,
   already in `Refusal`, and every start call reads it through
   `refusalMessage`. The comment line spec 132 sends at expiry is parsed and
   ignored, and the clean end after it reconnects; the stream test sends it.
6. **Spec 116's routes and spec 109's.** `Refusal` gains
   `CredentialRefusalOut` and `TelegramRefusalOut`, and `refusalMessage`
   reads a credential refusal's `field` and `problem`. Before this it read
   `loc` on every item and would have thrown on that body, which is the gap
   spec 109 recorded for the web. The two type pins in
   `OutreachPage.test.tsx` and `TrackerPage.test.tsx` name the new members.
   The credentials form calls `PUT /credentials/ai`, `/apify` and `/hunter`,
   which the contract declares one by one, and `DELETE /credentials/{kind}`.
7. **Spec 109's inbox criterion.** `InboxPage` reads the archive through
   `refusalMessage`, so the hosted 501 shows in the API's words, and the
   comment that said the route declares no refusal is corrected:
   "a hosted inbox says Gmail watch is not offered" in
   `InboxPage.test.tsx`.
8. **Spec 110's part.** The four variable names are documented in
   `.env.example`, in comments, with a line saying Vite does not read that
   file: it reads the build's environment and `apps/web/.env*`. No env file
   is added. `apps/web/.env.local` is still not covered by the
   classification table, which spec 110 records and owns.
9. **Hosted tests are their own files.** The mode is read when a module is
   first imported, as in a real bundle, so a test file that needs the hosted
   app sets the four variables with `vi.hoisted` before anything is imported.
   The tests the criteria planned for `App.test.tsx` and
   `SettingsPage.test.tsx` are in `App.hosted.test.tsx`,
   `App.misconfigured.test.tsx` and `SettingsPage.hosted.test.tsx`, and the
   local tests stay where they were.
10. **The retry wait is tested by holding its timer.** `holdTimers` in
    `shared/testing/runStreams.ts` keeps every timer set for exactly the
    retry delay until the test releases it and leaves the rest real, so
    `waitFor` and `userEvent` keep their clock. Fake timers were planned and
    would have stopped both.

11. **A secret in any `VITE_` variable reached the bundle.** The first build
    passed `import.meta.env` whole to `readAppMode`, and Vite copies a whole
    `import.meta.env` into the bundle, so every `VITE_` variable in the build's
    environment shipped, and the secret-key refusal ran only on one variable in
    hosted mode (privacy review of PR #328, reproduced with real builds). The
    app now reads the four variables by name, and `readAppMode` refuses a
    secret key in either mode. Checked by hand: an unrelated `VITE_` variable
    is in neither bundle, and a secret key in the key variable stops a local
    build. The `vite.config.ts` of this change loads the environment from the
    config file's own directory, not `process.cwd()`.
12. **A refresh the server refuses lost its notice.** supabase-js removes the
    session and fires `SIGNED_OUT` before `refreshSession` returns, so the
    sign-in page showed nothing and the first fake, which did not do this,
    hid it (test integrity review of PR #328). The notice is now set before
    the refresh, and the fake behaves as the library does.
13. **A form could stick.** Neither credentials nor Telegram form handled a
    request that got no answer, so the button stayed disabled until a reload
    and the Telegram secrets stayed in the fields (fsd review of PR #328).
    Both now report it and stay usable.
14. **The retry.** A retry's own network failure said "Failed to fetch"
    instead of the sentence for it, and no test showed that a retried write
    reads its body again. Both are fixed and tested.
15. **Test isolation.** `vitest.config.ts` sets `envDir: false`, so a
    developer's `apps/web/.env.local` cannot decide a local test's mode, and
    the config tests state all four variables themselves.

### The local reviews of PR 328

Four local reviews ran on the draft (contract, fsd, privacy, test integrity).
Each finding was read against the code, and the ones that reproduced are
items 11 to 15 above, with a test that fails without the fix. What was
declined or recorded elsewhere:

- **Declined.** The stream's uncapped retry on a 403, 422 or 5xx (fsd 3): it is
  what Behavior specifies, and is listed under "noticed and left". The render
  phase clear in `AuthGate` (fsd 2) is idempotent and now has a test under
  StrictMode, so it stays. `shared/README.md` (fsd 6) and the duplicate
  `UNREACHABLE` name (fsd 7, renamed) are not behavior.
- **Recorded, not changed.** `Last-Event-ID` as an undeclared header, the hosted
  500 body, the generated operation ids, and the hand-typed `GET /session`
  (contract B, C, D, F), all under "noticed and left". The email in
  supabase-js's own stored session (privacy 2) is under Honest limitations.
- **Fixed by wording.** Two citations claimed more than their tests prove (test
  integrity 8), and the criterion about the order of sign-out now says what
  the test asserts.
- **Kept, with the reason.** Four negative checks wait a fixed time (test
  integrity 9); a condition to wait for does not exist for "nothing was
  asked". Listed under "noticed and left".

### Decisions the text left open

- **Reconnecting.** After a clean end, the hook reconnects at once. It waits
  the retry delay only when two attempts in a row ended clean with no event,
  which is "also ends clean having delivered no event" in Behavior. An event
  at or before the last id received is not shown again, so a server that
  replays is harmless. A failure sets `disconnected`, refetches the run and
  waits the retry delay, and a 401 or a 404 stops the hook.
- **Ending a session.** `endSession` does nothing when the session is
  already gone, and concurrent callers share one `signOut`, which is what
  "three concurrent refusals sign the person out once" in `client.test.ts`
  needs. Concurrent
  refreshes share one call, since a refresh token is single use.
- **The gate.** It shows "Checking your session." until Supabase reports its
  stored session, then the sign-in page or the app. The app is keyed by the
  user's id, so a different person never inherits the old one's component
  state, and the cache is cleared in the render that first sees the new id.
- **The notice.** A session that ended under the person says why on the
  sign-in page, with the two sentences in Behavior; a sign-out the person
  asked for says nothing.
- **Misconfigured builds.** The page shows the refusal's text, which names the
  variable and never a key's value. The client module still builds, with the
  local shape, so that importing it cannot throw, and it sends nothing.

### Honest limitations of this change

- The hosted stream is read through `fetch` in jsdom and Node, and in a real
  browser only against the local demo, where a run streamed to its end and the
  reader aborted at the terminal state. A browser behind Fly's proxy is spec
  110's staging check.
- The two key shapes the build refuses are all it knows (Honest limitations,
  above); a future secret format would pass.
- The one Supabase behavior the code relies on that no test can show is that
  an unknown address and a known one answer alike. The tests fake the
  client, so they prove the page says the same sentence, not what Supabase
  returns.
- A refused local token is now the API's sentence. That is the one change a
  local user can see besides the `disconnected` notice and the badge.

### Out of scope, noticed and left

- `apps/web/.env.local` and `apps/web/.env.*` are in no classification entry
  (spec 110).
- `Last-Event-ID` is sent through free-form `headers` and is not a declared
  header parameter of `streamRunEvents`, though the API reads it and CORS
  allows it. Declaring it is a contract change to a route, for the spec that
  next changes that route.
- The hosted API's own 500, `COULD_NOT_RUN_AS_USER`, is a `{"detail": ...}`
  body that passes CORS and is declared on no route but one (spec 134
  owns the 500s).
- Six credentials operations carry FastAPI's generated operation ids, which
  ADR-005 says should be explicit (spec 116's routes, from before this
  change).
- `GET /session` is typed by hand in `shared/api/client.ts`, as it was before
  this change, because the local-token middleware cannot call itself.
- `shared/README.md` still describes `shared` as the client wrapper and the
  UI kit, and does not name `auth/`, `config/`, `testing/` and `track/`.
- The stream hook retries without a cap on any status but 401 and 404, which
  is what Behavior says ("until the run is terminal ... a new run begins, or
  the component unmounts"). A 403, 422 or 5xx on the stream is retried every
  3 seconds until then.
- Four tests check that nothing happened by waiting a fixed 30 to 50
  milliseconds. A request that landed later would go unseen on a slow runner.
- The `OperationsPage.test.tsx` assertion that the old token sentence is
  absent can no longer fail, since nothing renders it. It is left alone.
