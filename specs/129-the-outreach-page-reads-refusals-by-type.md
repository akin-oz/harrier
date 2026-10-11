---
spec: 129
title: The outreach page reads a refusal through the contract's types
status: proposed
approved: no
milestone: M8
depends: [048, 082, 094, 125]
---

# Spec 129: The outreach page reads a refusal through the contract's types

## Problem

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
  - `refused` takes the error bodies the contract declares, by their
    generated names, with no `unknown` and no cast. It is exported so a
    test can pin its parameter.
  - `unwrap` takes a result whose `error` is that same union.
  - The comment above `unwrap` says what is true now: the bodies are
    declared, and the `data === undefined` check stays for the 403, which
    still declares none.
- `apps/web/src/pages/outreach/OutreachPage.test.tsx`: a type-level test,
  and rendered refusal tests for the call sites that have none.
- `specs/082-tracker-refusals-are-in-the-contract.md`: its Out of scope
  entry for the pages' readers is amended in the implementing change to
  say the outreach page's is done here.

No API change, no contract change, and no change to what the operator
sees. `ApplyPage.tsx`, `InboxPage.tsx`, the run panels and `JobActions.tsx`
are not touched.

## Behavior

### The reader

```ts
type Refusal =
  | components["schemas"]["ErrorOut"] // 404, 409
  | components["schemas"]["DatabaseHeldOut"] // 503
  | components["schemas"]["HTTPValidationError"]; // 422

export function refused(error: Refusal, fallback: string): string {
  return typeof error.detail === "string" ? error.detail : fallback;
}
```

- It returns `detail` when that is a string (`ErrorOut`,
  `DatabaseHeldOut`) and the call site's fallback otherwise. A 422's
  `detail` is a list of field errors, which only a malformed request from
  this page can produce, so it reads as the fallback, as today.
- Each call site keeps its own fallback ("could not load the due queue",
  "could not mark sent", and so on).
- What the operator sees does not change for any status.

### What now fails `pnpm type-check`

1. **`ErrorOut` or `DatabaseHeldOut` loses or renames `detail`.** Reading
   `detail` from the union no longer compiles, or a call's error no longer
   fits `Refusal`.
2. **A call the page makes declares an error body with no `detail`.** Its
   result no longer fits `unwrap`'s parameter, or its `error` no longer
   fits `refused`'s.

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
  runtime FastAPI sends `{"detail": ...}`, which `refused` reads as it
  reads any other body. The mutations' `error !== undefined` guard and
  `unwrap`'s `data === undefined` guard both stay.
- **An error with an empty body.** openapi-fetch returns neither `error`
  nor `data`. `unwrap` throws its fallback, as today. The mutations ignore
  `data`, so an empty-bodied refusal of a write is read as success, as
  today (Out of scope).

## Acceptance criteria

- [ ] `refused` takes `ErrorOut`, `DatabaseHeldOut` and
      `HTTPValidationError` by generated name, with no `unknown` and no
      cast. A type-level test pins its parameter to that union, and pins
      the 404 and 409 of each of the page's eight `{selector}` and `due`
      operations to `ErrorOut` and every 503 to `DatabaseHeldOut`, using
      vitest's `expectTypeOf`, which `pnpm type-check` compiles (planned
      OutreachPage.test.tsx::"an outreach refusal is read through the
      contract's types"). Without the change it does not compile:
      `refused` is not exported.
- [ ] Each call site shows a refusal's `detail` verbatim: the due queue,
      the contacts list and the candidates list for a read, and Mark sent,
      Mark replied, Snooze, Reject and Find contacts for a write (planned
      OutreachPage.test.tsx::"each outreach call shows its refusal in the
      API's words"). The existing approval test already covers Approve.
      This passes before the change too. It pins that typing the reader
      did not change what the page shows.
- [ ] The implementing pull request shows the type-level test failing to
      compile on main's code, and records what was tried (its description,
      and an amendment here).
- [ ] The comment above `unwrap` no longer says the refusals declare no
      body.
- [ ] Spec 082's Out of scope entry for the pages' readers says the
      outreach page's is done here.
- [ ] `pnpm type-check`, `pnpm lint` and the web tests pass under Node 22,
      and `just check` passes.
- [ ] No real person or application appears in a fixture (ADR-008). The
      new cases use the suite's invented candidate and job.

## Honest limitations

- **The operator sees no difference.** Every message shown before is shown
  after. What changes is that drift in `detail` fails a check instead of
  degrading to the fallback.
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
- **One shared reader.** `JobActions.tsx`'s `refusalMessage` and this
  `refused` differ only in their fallback. Moving one reader into
  `apps/web/src/shared/api` touches the tracker page, which this spec does
  not (open decision 1).
- **An empty-bodied refusal of a write reads as success.** The mutations
  check `error` and not `data`. No route sends an empty refusal today.
- **The 403's body**, as spec 082 left it.

## Open decisions for Akin

1. **A local reader or a shared one.** Proposed: local, in
   `OutreachPage.tsx`, as spec 082 did for the tracker page. Sharing it
   means changing `JobActions.tsx` and the tracker page's type-level test,
   which is a second page's change. The alternative is one reader in
   `shared/api` now, used by both pages.
2. **The name.** Proposed: keep `refused`, exported. The alternative is to
   rename it to `refusalMessage` to match the tracker page, which is a
   rename in a change that is otherwise only types.

## Data and privacy

No data changes. The tests stub bodies with invented messages and the
suite's invented candidate (`linkedin.com/in/` URL that names no one).

## Migration

None.

## Proof / origin

- `apps/web/src/pages/outreach/OutreachPage.tsx`: `refused(error:
  unknown, ...)` and its cast; the comment above `unwrap`.
- `packages/contract/openapi.json` on main at `06420bb`: the table in
  Problem.
- `apps/web/src/features/tracker/JobActions.tsx`: `refusalMessage`, spec
  082's typed reader, the precedent.
- Spec 082's Out of scope entry naming this page's reader. Specs 094 and
  125: the declarations that make the typed reader possible.
- A throwaway edit while this spec was written: with `refused` typed and
  `unwrap` taking `Refusal`, `pnpm type-check` passed on main's contract;
  with `DatabaseHeldOut` removed from the union, it still passed.
