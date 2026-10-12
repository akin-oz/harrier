---
spec: 129
title: The outreach page reads a refusal through the contract's types
status: accepted
approved: yes
milestone: M8
depends: [048, 082, 094, 125]
---

# Spec 129: The outreach page reads a refusal through the contract's types

## Problem

*As the page stood on main at `06420bb`, before this spec. The page has
since dropped this reader (the amendments below).*

The outreach page shows a refusal in the API's words (spec 048). It reads
those words through `refused` in
`apps/web/src/pages/outreach/OutreachPage.tsx`, which takes `unknown` and
casts it to a hand-written `{ detail?: unknown }`. ADR-005 exists to rule
that out: the browser reads a field the contract does not name, so `tsc`
cannot see it.

Spec 082 fixed the same reader on the tracker page and left this one as out
of scope. At the time the cast had a reason. The outreach refusals declared
a status and no body, so the generated error type had nothing to read. The
comment above `unwrap` still says so.

It is no longer true. Spec 094's route class gave every scoped route an
`ErrorOut` 404 and 409, and spec 125 made the outreach declarations list
only what each route sends. Every call this page makes now declares its
error bodies by name (read from `packages/contract/openapi.json` on main at
`06420bb`):

| Operation | Page call | 404 | 409 | 422 | 503 |
|---|---|---|---|---|---|
| `listOutreachDue` | `fetchDue` | `ErrorOut` | `ErrorOut` | `HTTPValidationError` | `DatabaseHeldOut` |
| `listOutreachContacts` | `fetchContacts` | none | none | none | `DatabaseHeldOut` |
| `listCandidates` | `fetchCandidates` | `ErrorOut` | `ErrorOut` | `HTTPValidationError` | `DatabaseHeldOut` |
| `markOutreachSent` | `mark` | `ErrorOut` | `ErrorOut` | `HTTPValidationError` | `DatabaseHeldOut` |
| `markOutreachReplied` | `mark` | `ErrorOut` | `ErrorOut` | `HTTPValidationError` | `DatabaseHeldOut` |
| `snoozeOutreach` | `snooze` | `ErrorOut` | `ErrorOut` | `HTTPValidationError` | `DatabaseHeldOut` |
| `approveCandidate` | `decide` | `ErrorOut` | `ErrorOut` | `HTTPValidationError` | `DatabaseHeldOut` |
| `rejectCandidate` | `decide` | `ErrorOut` | `ErrorOut` | `HTTPValidationError` | `DatabaseHeldOut` |
| `findContacts` | `findContacts` | `ErrorOut` | `ErrorOut` | `HTTPValidationError` | `DatabaseHeldOut` |

Each also declares a 403 with no body (spec 082, open decision 1).

What the cast costs:

- **Nothing fails if the body changes.** If `ErrorOut` lost or renamed
  `detail`, `just contract` would show the diff, but `pnpm type-check`
  would pass, because the reader takes `unknown`. The page tests would
  pass too, because `stubApi` in `OutreachPage.test.tsx` writes each body
  by hand. The operator would see the fallback instead of the reason.
- **One of nine call sites has a browser proof.** `OutreachPage.test.tsx`
  shows a refused approval in the API's words. No test shows a refused
  read, mark, snooze, rejection or discovery.

## Scope

- `apps/web/src/pages/outreach/OutreachPage.tsx`:
  - The page's own `refused` is removed. Every call site reads a refusal
    through `refusalMessage` in `apps/web/src/shared/api/refusal.ts`, which
    takes the error bodies the contract declares by their generated names
    (amended in review of PR #278).
  - `unwrap` takes a result whose `error` is that reader's `Refusal`.
  - The comment above `unwrap` says what is true now: the bodies are
    declared, and the `data === undefined` check stays for the 403, which
    still declares none.
- `apps/web/src/pages/outreach/OutreachPage.test.tsx`: a type-level test,
  and rendered refusal tests for the call sites that have none.
- `specs/082-tracker-refusals-are-in-the-contract.md`: its Out of scope
  entry for the pages' readers is amended in the implementing change to
  say the outreach page's is done here.

No API change and no contract change. What the operator sees changes only
for a 422 (Behavior). `ApplyPage.tsx`, `InboxPage.tsx`, the run panels and `JobActions.tsx`
are not touched.

## Behavior

### The reader

The page uses the shared reader, `refusalMessage(error: Refusal,
fallback: string)` in `apps/web/src/shared/api/refusal.ts`, whose
`Refusal` is `ErrorOut | DatabaseHeldOut | HTTPValidationError` by
generated name. Eighteen other files call it: eleven import it directly,
and seven reach it through `apps/web/src/pages/operations/RunOutput.tsx`,
which re-exports it. Its union is the same as the page's old one; its
handling of a 422 is not.

- It returns `detail` when that is a string (`ErrorOut`,
  `DatabaseHeldOut`).
- A 422's `detail` is a list of field errors. The shared reader names each
  field and its message, joined by "; ", and shows the call site's
  fallback only when the list is empty. The page's old reader showed the
  fallback for every 422. Only a malformed request from this page can
  produce a 422, so the operator should never see one; if they do, they
  now see which field was refused. The test's "until: an invented field
  error" is a hand-stubbed illustration: `SnoozeIn.until` is any string,
  the page sends only a non-empty one, and a date the domain cannot parse
  is a 409 shown in the domain's words, before and after this spec.
- Each call site keeps its own fallback ("could not load the due queue",
  "could not mark sent", and so on).
- For every status but a 422, what the operator sees does not change.

### What now fails `pnpm type-check`

1. **`ErrorOut` or `DatabaseHeldOut` loses or renames `detail`.** Reading
   `detail` from the union no longer compiles, or a call's error no longer
   fits `Refusal`.
2. **A call the page makes declares an error body with a required field
   the union cannot hold** (for example `{ message: string }`). Its result
   no longer fits `unwrap`'s parameter, or its `error` no longer fits
   `refusalMessage`'s. A body with no required field, such as an empty
   object, still fits, because `HTTPValidationError.detail` is optional;
   only the statuses the type-level test pins catch that (Honest
   limitations).

A new body that carries `detail: string` still compiles, because
TypeScript compares shapes, not names. The reader reads such a body
correctly, so nothing is lost (Honest limitations).

## Failure modes

- **A body FastAPI did not write.** An unhandled exception answers 500
  with `text/plain`. openapi-fetch hands it to the reader as a string,
  which has no `detail` property, so the fallback shows, as today. The
  types do not describe this body; ADR-005 adds no runtime validation, and
  this spec keeps that.
- **A 403.** It declares no body, so by type the error is `undefined`. At
  runtime FastAPI sends `{"detail": ...}`, which `refusalMessage` reads as
  it reads any other body. The mutations' `error !== undefined` guard and
  `unwrap`'s `data === undefined` guard both stay.
- **An error with an empty body.** It depends on the headers. On a 204,
  a HEAD, or `Content-Length: 0` without chunked encoding, openapi-fetch
  returns neither `error` nor `data`: `unwrap` throws its fallback (the
  due-queue case in the test proves it), and a mutation, which ignores
  `data`, reads the refusal as success (Out of scope). Otherwise `error`
  is the empty string, which passes the guards and shows the fallback.
- **A JSON `null` body.** openapi-fetch parses an error body with
  `JSON.parse`, so a body of `null` arrives as `error === null`, passes the
  `!== undefined` guards, and `refusalMessage` throws reading `detail`. The
  operator would see a `TypeError` message. The page's reader before
  PR #278 returned the fallback; the typed `refused` that #278 shipped
  already threw. No route sends `null`, and the shared reader has the
  same gap for all eighteen of its other callers (Out of scope).

## Acceptance criteria

- [x] The page has no reader of its own, no `unknown` and no cast: every
      call site reads through the shared `refusalMessage`. That much is
      held by review, not by a test (Honest limitations). A type-level
      test pins that reader's parameter to `ErrorOut`, `DatabaseHeldOut`
      and `HTTPValidationError` by name, and pins the 404 and 409 of each
      of the page's eight `{selector}` and `due` operations to `ErrorOut`
      and every 503 to `DatabaseHeldOut`, using vitest's `expectTypeOf`,
      which `pnpm type-check` compiles
      (OutreachPage.test.tsx::"an outreach refusal is read through the
      contract's types").
- [x] Each call site shows a refusal's `detail` exactly: the due queue,
      the contacts list and the candidates list for a read, and Mark sent,
      Mark replied, Snooze, Approve, Reject and Find contacts for a write,
      one case each. A 422 on Snooze shows its field error, a 422 that
      names no field shows "could not snooze", and a due-queue refusal
      with no body shows "could not load the due queue". Snooze sends the
      date the operator gave, for the row it was given on (the cases
      generated from `REFUSALS` in
      `apps/web/src/pages/outreach/OutreachPage.test.tsx`, one per row,
      titled "each outreach call shows its refusal in the API's words"
      and the row's call; a title built at run time has no fixed name the
      citation check can resolve, so renaming them fails no check). On
      the page #278 shipped, the 422 case fails, because that reader
      shows the fallback; every other case passes there too.
- [x] The implementing pull request records what was run against main's
      code and what failed (its description, and an amendment here).
- [x] The comment above `unwrap` no longer says the refusals declare no
      body.
- [x] Spec 082's Out of scope entry for the pages' readers says the
      outreach page now uses the shared reader.
- [x] `pnpm type-check`, `pnpm lint` and the web tests pass under Node 22,
      and `just check` passes.
- [x] No real person or application appears in a fixture (ADR-008). The
      new cases use the suite's invented candidate and job.

## Honest limitations

- **The operator sees one difference, on a 422.** Every other message
  shown before is shown after. What changes is that drift in `detail`
  fails a check instead of degrading to the fallback.
- **The type-level test pins the shared reader, not the page's use of
  it.** The 422 case is what tells the shared reader from the page's old
  one. A page that went back to a reader of its own which also named a
  422's fields would pass.
- **A body with no required field fits the union.** Because
  `HTTPValidationError.detail` is optional, an error body the generator
  emits as an empty object still fits `Refusal`, so `pnpm type-check`
  passes at the page. Only a status the type-level test pins fails on it.
  The comment in `apps/web/src/shared/api/refusal.ts` says a changed body
  fails the type check there, which overstates the same way (Out of
  scope).
- **Shapes, not names.** `DatabaseHeldOut` fits `ErrorOut`, because both
  carry `detail: string`. Removing `DatabaseHeldOut` from `Refusal` still
  compiles; this was tried with a throwaway edit while this spec was
  written. The type-level test is what pins the union by name.
- **The 403 is still untyped,** as spec 082 left it.
- **The rendered tests stub each body by hand.** They prove the page shows
  what it is given, not that the API sends that body. Spec 125's pairing
  tests prove the API side.

## Out of scope

Each is recorded here for its own change.

- **The other pages' readers.** `ApplyPage.tsx` and `InboxPage.tsx` keep
  their untyped copies, and `useRunStream.ts` and `RunPanel.tsx` show a run
  error as `JSON.stringify(error)`.
- **`JobActions.tsx` keeps its own reader.** The tracker page's
  `refusalMessage` predates the shared one and differs from it on a 422.
  Moving it to the shared reader changes the tracker page, which this spec
  does not touch.
- **A JSON `null` refusal body** throws in the shared reader for every
  caller (Failure modes). No route sends one.
- **An empty-bodied refusal of a write reads as success** when it is a
  204 or carries `Content-Length: 0`. The mutations check `error` and not
  `data`. No route sends an empty refusal today.
- **The comment in `apps/web/src/shared/api/refusal.ts`** says a body the
  contract changes fails the type check there. A body with no required
  field does not (Honest limitations). The file is the shared reader's,
  outside this page.
- **The 403's body**, as spec 082 left it.

## Decided in review (2026-10-11)

Akin answered both in chat, each as proposed.

1. **A local reader.** `refused` stays in `OutreachPage.tsx`, as spec 082
   kept the tracker page's in `JobActions.tsx`. One shared reader is its
   own change, because it touches the tracker page. *Revised in review of
   PR #278; see the amendment below.*
2. **The name stays `refused`,** exported. No rename. *Moot after the
   revision of decision 1: the page has no reader of its own.*

## Data and privacy

No data changes. The tests stub bodies with invented messages and the
suite's invented candidate (`linkedin.com/in/` URL that names no one).

## Migration

None.

## Proof / origin

- `apps/web/src/pages/outreach/OutreachPage.tsx` on main at `06420bb`:
  `refused(error: unknown, ...)` and its cast; the comment above
  `unwrap`.
- `packages/contract/openapi.json` on main at `06420bb`: the table in
  Problem.
- `apps/web/src/features/tracker/JobActions.tsx`: `refusalMessage`, spec
  082's typed reader, the precedent.
- Spec 082's Out of scope entry naming this page's reader. Specs 094 and
  125: the declarations that make the typed reader possible.
- A throwaway edit while this spec was written: with `refused` typed and
  `unwrap` taking `Refusal`, `pnpm type-check` passed on main's contract;
  with `DatabaseHeldOut` removed from the union, it still passed.

## Amendment (2026-10-11, during implementation)

What implementation found, each with its proof. Scope and behavior are as
specified.

- **The stub gained one option.** `stubApi` in `OutreachPage.test.tsx`
  takes `refuse`, a status and body for the route whose path ends a given
  way, answered before any other stub. Without it every route answers as
  before, so no existing test sees a different answer.
- **The rendered test covers eight call sites in one test.** *Superseded:
  one test per case since the amendment for PR #278.* Each case
  renders the page, refuses one call with an invented message, and finds
  that message in an alert. Snooze stubs `prompt` with a date.
- **Each new test was run against main's page code.** With
  `OutreachPage.tsx` put back to main, `pnpm type-check` fails: the page
  declares `refused` locally and does not export it. The rendered test
  passes there, as this spec said it would.
- **The type-level test fails on the old reader alone.** With main's
  reader exported but still taking `unknown`, `pnpm type-check` fails at
  `expectTypeOf(refused).parameter(0)`. Both were temporary edits, run and
  reverted. *Superseded: the test now pins the shared reader, and `refused`
  is gone (amendment for PR #278).*

## Amendment (2026-10-11, review of PR #278)

CodeRabbit was rate limited on PR #278, so the repository's review agents
reviewed it locally. Their findings, each checked against the code, and
what was done.

- **A shared reader already existed.** `apps/web/src/shared/api/refusal.ts`
  has exported the same `Refusal` union and a `refusalMessage(error,
  fallback)` that differs from the page's only on a 422, since 2026-10-10
  (commit `fe22c11`), and eighteen other files call it. This spec did not know it existed, and decision 1's reason
  ("sharing touches the tracker page") was wrong. Akin chose the shared
  reader in chat on 2026-10-11, knowing it shows a 422's field errors
  where the page's reader showed the fallback. The page's `refused` is
  removed; Scope, Behavior, Failure modes, the criteria, Honest
  limitations and Out of scope are amended to match.
- **What fails on main changed with it.** The type-level test now pins
  the shared reader, which exists on main, so main's page compiles
  against it. With `OutreachPage.tsx` put back to main, `pnpm type-check`
  passes and one rendered case fails: the 422 on Snooze, which main's
  reader shows as "could not snooze". The previous amendment's
  "`refused` is not exported" evidence no longer applies.
- **The rendered test is one case per call site,** so one call site
  failing no longer hides the rest. The spec citation check in
  `services/api/tests/test_spec_structure.py` reads only fixed
  `test("...")` names, so the generated cases are named in prose.
- **The alert must equal the API's words.** A `contains` check let a
  page that wrapped the message pass. With Snooze's message prefixed by
  "Refused: ", both Snooze cases now fail.
- **Snooze proves the date it sends.** jsdom's `prompt` returns
  `undefined`, which the page let through, so the `prompt` stub did
  nothing for the result. The cases now assert the request body is
  `{ until: "2026-12-01" }`; without the stub both Snooze cases fail.
- **The stubbed bodies are typed.** They are `ErrorOut`,
  `DatabaseHeldOut` or `HTTPValidationError` by generated name, and the
  cast to a hand-written `{ detail: string }` is gone.
- **A JSON `null` body throws.** Confirmed in openapi-fetch's source; it
  is recorded under Failure modes and Out of scope, because the gap is
  the shared reader's and affects every caller.
- **The hold's timestamp in the test** is now `2000-01-01T00:00:00Z`, so
  it cannot be read as a real hold. The previous one was invented, not
  copied from a hold.

## Amendment (2026-10-11, review of PR #283)

PR #283 merged while CodeRabbit was rate limited, so the repository's
review agents reviewed it locally afterwards. Their findings, each checked
against the code, and what was done.

- **The count was wrong.** Twenty-three files contain the name
  `refusalMessage`, but four of them use other readers (`JobActions.tsx`,
  which `AddJob.tsx` and `TrackerPage.test.tsx` import, and
  `ApplyPage.tsx`'s own), and `RunOutput.tsx` only re-exports the shared
  one. Eighteen other files call it. Corrected throughout.
- **The loop's stated reason was false.** The PR #278 amendment said a
  loop of `test` calls let the citation check read the names. Their names
  are template literals, which the check skips as well. The cases are
  back to `test.each`, and the comment and the amendment say what is
  true: they are named in prose and no check reads their names.
- **Approve's test matched by containment.** It was the one call site
  outside the generated cases. Approve is now a case, matched exactly,
  and the older approval test matches exactly too. With "Refused: "
  prefixed on Approve only, both fail.
- **No case reached a fallback.** Two cases now do: a 422 with an empty
  list ("could not snooze") and a due-queue refusal with no body ("could
  not load the due queue"). With every fallback string replaced, both
  fail. The stub answers a body of `undefined` with no body and
  `Content-Length: 0`.
- **Snooze did not check its row.** The cases now assert the request went
  to `/api/outreach/12/snooze`. With the page snoozing `row.id + 1`, all
  three Snooze cases fail.
- **"No `unknown` and no cast" was claimed as tested.** It is held by
  review; the criterion now says so. A cast through `unknown` in `unwrap`
  still compiles and passes.
- **Type-check item 2 overclaimed.** An empty-object body fits the union,
  because `HTTPValidationError.detail` is optional. Behavior, Honest
  limitations and Out of scope now say so; the same overstatement in the
  comment in `shared/api/refusal.ts` is recorded, not changed, because
  that file is the shared reader's.
- **The empty-body failure mode was stated without its condition.** It
  holds on a 204 or `Content-Length: 0`; otherwise the error is the empty
  string and the fallback shows. Corrected.
- **The `null` baseline was wrong.** The typed `refused` from #278 already
  threw on `null`; only the reader before it returned the fallback.
  Corrected.
- **The 422 example cannot happen on this page.** Behavior now says the
  "until" example is a hand-stubbed illustration and that a bad date is a
  409.
- **Problem, Proof and the first amendment described code that is gone.**
  Each is marked as the state before, or superseded.
- **Spec 082 still called this page's reader untyped.** Its entry now
  says the page uses the shared reader.
