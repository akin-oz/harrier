---
spec: 163
title: A failed API answer is read as a failure, whatever its body
status: proposed
approved: no
milestone: M6
depends: [005, 082]
---

# Spec 163: A failed API answer is read as a failure, whatever its body

## Problem

The web app talks to the API through the openapi-fetch client in
`apps/web/src/shared/api/client.ts` (ADR-005). On 2026-10-20, 65 call sites
in `apps/web/src` decide that a request failed with
`if (error !== undefined)`. This finds them:

```bash
git grep -n "if (error !== undefined)" -- apps/web/src
```

openapi-fetch 0.17.0 does not always set `error` on a failed answer. In
`dist/index.mjs`, `coreFetch`:

- returns `{ error: undefined }` for a non-ok response with
  `Content-Length: 0` (and no chunked transfer encoding);
- returns `error` as the raw text for a non-ok body that is not JSON, for
  example an HTML 502 page from a proxy or FastAPI's `text/plain`
  "Internal Server Error";
- returns whatever `JSON.parse` gives for a JSON body, which can be `null`,
  a number or an array.

So an empty 500 or 502 is read as success. A mutation that answers that way
runs its success path: the page can say "Brief saved." when nothing was
saved, and a read hands `undefined` to a component that expects data.

`refusalMessage` in `apps/web/src/shared/api/refusal.ts` is typed to take a
contract refusal object. A string reaches it today and happens to give the
fallback. A JSON `null` reaches it too and throws a `TypeError` on
`error.detail`, so the operator sees that instead of the fallback.

A JSON object can also throw there. `refusalMessage` returns a string
`detail` as it is and otherwise calls `(error.detail ?? []).map(...)`. So
`{"detail": {"reason": "blocked"}}` throws (an object has no `map`), and so
does a list with an item that is not an object (`"loc" in "x"` throws on a
string). A list item that is an object of neither declared shape reads as
"undefined: undefined". The types say none of this can arrive. A proxy, a
route that raises `HTTPException` with a dict `detail`, or a contract the
browser has not caught up with can send it anyway.

Spec 082 named the empty body as a failure mode for the tracker writes and
relied on their `data === undefined` guard. Most call sites have no such
guard. No spec says how the browser reads a failed answer in general.

Who it hurts: the operator, who is told an action worked when it did not,
or sees a stack message instead of a sentence. The hosted deployment
(ADR-013) has a proxy in front of the API, where an empty or HTML 502 is
ordinary.

Found by CodeRabbit on PR #413, which fixed both shapes for the capture page
only (`refusalOf` in `apps/web/src/pages/capture/CapturePage.tsx`, deciding
on `response.ok` and guarding the `detail` it reads).

## Scope

Two places, and no call site changes:

- the generated client's middleware in
  `apps/web/src/shared/api/client.ts`, in both build modes;
- the shared reader `refusalMessage` in
  `apps/web/src/shared/api/refusal.ts`.

## Behavior

After the change, every response the client returns passes one rule before
openapi-fetch reads it:

- **A non-ok response whose body is a JSON object** is left as it is. Every
  refusal the contract declares (`ErrorOut`, `DatabaseHeldOut`,
  `HTTPValidationError`, `AuthRefusalOut`, `CredentialRefusalOut`,
  `TelegramRefusalOut`) is a JSON object, so each still reaches its call
  site unchanged and `refusalMessage` still shows the API's words.
- **A non-ok response whose body is anything else** is replaced by a
  response with the same status and status text, `Content-Type:
  application/json`, no `Content-Length` from the original, and the body
  `{}`. "Anything else" is: empty, not JSON (HTML, plain text), JSON that is
  not an object (`null`, a number, a string, an array), or a body that
  fails to read.
- **An ok response** is left as it is, including 204 and streams.

So for every failed answer, `error` is an object and `error !== undefined`
holds. `refusalMessage({}, fallback)` returns the call site's fallback. A
call site that prints `JSON.stringify(error)` prints `{}`.

The rule runs after the hosted middleware's refusal handling, so the hosted
middleware still reads the original body of a 401 or 403 (spec 114), and
the answer to its one retry after a refresh passes the rule too.

A request that gets no answer at all is unchanged: it rejects, and the
hosted build turns that into `UNREACHABLE` (spec 134).

`refusalMessage(error, fallback)` never throws, whatever object it is
given:

- a string `detail` is returned as it is, as today;
- a list `detail` is read item by item. An item that is an object with an
  array `loc` and a string `msg` reads as `<last loc part>: <msg>`, as
  today. An item that is an object with a string `field` and a string
  `problem` reads as `<field>: <problem>`, as today. Any other item is
  skipped. The read items are joined with "; ", and an empty result gives
  the fallback;
- any other `detail` (missing, `null`, a number, an object) gives the
  fallback.

Its parameter type stays the contract union of spec 082, so a contract
change still fails `pnpm type-check` there.

## Failure modes

- **An empty 500 or 502 on a write.** The call site's error path runs and
  shows its fallback. Its success path does not run.
- **An HTML 502 from a proxy.** The fallback shows, not the HTML.
- **A JSON `null` error body.** The fallback shows, not a `TypeError`.
- **A declared refusal.** Unchanged: the API's `detail` shows verbatim.
- **A JSON object whose `detail` is an object**, such as
  `{"detail": {"reason": "blocked"}}`. The fallback shows, not a
  `TypeError`.
- **A list `detail` with some items of neither shape.** The items that
  have a declared shape show; the others are dropped. If none has one, the
  fallback shows.
- **A hosted 401 with an empty body.** The hosted middleware reads it first
  as today (no reason, so the session ends as `invalid`), then the call
  site gets `{}` and shows its fallback.
- **The retry after a refresh answers an empty 500.** The call site gets
  `{}`, as for any other failed answer.
- **A 204 or a HEAD request.** Untouched: openapi-fetch returns
  `data: undefined` for an ok one, as today.
- **A failed answer on a stream or a download** (`parseAs: "stream"` or
  `"blob"`). openapi-fetch reads a failed body as text whatever `parseAs`
  says, so the same rule applies.

## Acceptance criteria

- [ ] In the local build, saving a brief that the API answers with an
      empty 500 (`Content-Length: 0`) shows the save's fallback and never
      "Brief saved." (planned test in BriefPanel.test.tsx: "an empty failed
      answer is not read as a saved brief"). It fails without the change.
- [ ] In the local build, reading a brief that the API answers with a 502
      whose body is an HTML page shows the read's fallback and no HTML
      (planned test in BriefPanel.test.tsx: "a failed answer that is not
      JSON shows the fallback").
- [ ] In the hosted build, a request answered with an empty 500, a
      `text/html` 502, and a JSON `null` 500 each returns an `error` that is
      the object `{}` with the original status on `response.status`; a 409
      `ErrorOut` body is returned unchanged (planned test in
      client.test.ts: "a failed answer with no JSON object body becomes an
      empty refusal"). The empty case fails without the change.
- [ ] In the hosted build, a 401 `expired` whose retry after a refresh
      answers an empty 500 returns `error` as `{}` (planned test in
      client.test.ts: "the retry's empty failed answer becomes an empty
      refusal").
- [ ] `refusalMessage` returns the fallback for `{"detail": {"reason":
      "blocked"}}`, for `{"detail": null}`, for `{"detail": 3}` and for
      `{"detail": ["x", null]}`; and for `{"detail": ["x", {"loc":
      ["body", "limit"], "msg": "too large", "type": "x"}, {"other": 1}]}`
      it returns "limit: too large" (planned test in refusal.test.ts: "a
      detail of no declared shape gives the fallback, never a throw"). The
      first and fourth cases throw without the change.
- [ ] The existing tests in `refusal.test.ts` pass unchanged.
- [ ] An existing test that shows a declared refusal verbatim still passes
      (`BriefPanel.test.tsx`: "a refusal is shown in the store's words and
      the input is kept").
- [ ] The implementing pull request shows the new tests failing on main's
      code.
- [ ] `pnpm type-check`, `pnpm lint`, `pnpm format:check` and the web tests
      pass under Node 22, as CI runs them.

## Proof / origin

- openapi-fetch 0.17.0, `dist/index.mjs`, the end of `coreFetch` (lines
  179 to 201 in the installed copy).
- CodeRabbit's findings on PR #413 about the capture page: the `in`
  operator on a string error body, and `refusalMessage` throwing on an
  object `detail`.
- Spec 082, Failure modes, which named the empty body for the tracker
  writes.

## Out of scope

- Rewording the call sites that print `JSON.stringify(error)` (for example
  `ArtifactList.tsx`, `RunPanel.tsx`'s cancel, `useRunStream.ts`). They
  print `{}` after this change instead of an empty string or a page of
  HTML. Giving them a sentence is a separate change.
- Showing the HTTP status in the fallback. Each call site's fallback stays
  as written.
- Replacing `if (error !== undefined)` at the call sites. The rule makes it
  correct, so the 65 sites stay as they are.
- Runtime validation of a JSON object body against its declared schema.
  ADR-005 trusts the types at the seam, and spec 082 kept that.
- The capture page's own `refusalOf` (PR #413). It decides on
  `response.ok`, guards its own `detail`, and stays correct after this
  change. Moving it onto the shared reader is a separate change.
- `localToken`'s `GET /session` in `client.ts`, which uses `fetch`
  directly and already decides on `response.ok`.

## Honest limitations

- A non-ok answer whose body is a JSON object the contract does not
  declare passes through as it is. `refusalMessage` shows its `detail` if
  that is a string, and the fallback otherwise.
- The rule reads every failed body once more than before, through a
  clone. Failed bodies are small; no number is claimed for the cost.

## Migration

None. The operator sees a fallback sentence where they used to see a false
success, a `TypeError`, or a page of HTML.
