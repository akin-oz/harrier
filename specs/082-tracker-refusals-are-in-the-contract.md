---
spec: 082
title: Tracker writes declare the refusals they send, and the browser reads them by type
status: accepted
approved: yes
milestone: M8
depends: [005, 042, 080]
---

# Spec 082: Tracker writes declare the refusals they send, and the browser reads them by type

## Problem

The three tracker writes that name a job refuse with a 404 or a 409 and a
JSON body, `{"detail": "<message>"}`. The contract declares the two statuses
and not the body.

`TRACKER_ERRORS` in `services/api/src/harrier_api/app.py` gives 404 and 409 a
description and no model. So `packages/contract/openapi.json` has no
`content` for them, and `packages/contract/src/schema.d.ts` types their
content as `never`. To the generated client, a 409 from `changeJobStatus`
has no body.

The body exists, and the browser depends on it. Specs 042 and 080 require a
refusal to be shown in the domain's own words. `refusalMessage` in
`apps/web/src/features/tracker/JobActions.tsx` does that by taking `unknown`
and casting it to a hand-written `{ detail?: unknown }`. ADR-005 exists to
rule that out: the browser reads a field the contract does not declare, and
`tsc` cannot see it.

What that costs, and who pays:

- **Nothing fails if the body changes.** If a route sent its refusal in
  another shape, the contract would not change, so `just contract` would show
  no diff. `pnpm type-check` would pass, because the browser reads `unknown`.
  The browser tests would pass, because `stubApi` in
  `apps/web/src/pages/tracker/TrackerPage.test.tsx` writes each body by hand.
  The operator would see the fallback, "the tracker refused that change",
  instead of the reason. That is the drift spec 042 exists to prevent, moved
  to the error path.
- **A checked criterion of spec 042 is not true.** It says no hand-written
  request or response shape appears in `apps/web`, proved by the contract
  drift gate. The gate cannot see a shape the contract never declared.
- **Four of the six refusals have no browser proof.** The browser tests show
  a 409 from status and one from outcome. Nothing shows a rescore refusal, or
  any 404.

A contract review of the merged spec 077 to 081 range found this. It is not
spec 080's behavior, so it was left out of that spec's review fixes
([PR #127](https://github.com/akin-oz/harrier/pull/127)) and is specified
here.

## The refusals today

Read from `app.py` and `harrier.tracker.actions`, then each row run against a
scratch database through `TestClient`.

| Operation | Status | When | Body | Declared | Proved by |
|---|---|---|---|---|---|
| `changeJobStatus` | 404 | `SelectorError`: no row, more than one, or an empty selector | `{"detail": str}` | description only | `test_a_selector_matching_nothing_is_a_404` (status code only) |
| `changeJobStatus` | 409 | `TrackerActionError` or `TrackerError`: an unknown verb, a reason or code on a move that is not a rejection, a store refusal | `{"detail": str}` | description only | `test_an_unknown_verb_is_refused`, `test_a_reason_on_a_non_rejection_is_refused` |
| `recordCompanyOutcome` | 404 | `SelectorError` | `{"detail": str}` | description only | none |
| `recordCompanyOutcome` | 409 | no application, invited interview or earlier company response recorded (spec 079) | `{"detail": str}` | description only | `test_company_outcome_refuses_an_unapplied_row` |
| `rescoreJob` | 404 | `SelectorError` | `{"detail": str}` | description only | none |
| `rescoreJob` | 409 | no stored description (spec 033) | `{"detail": str}` | description only | `test_a_job_with_no_stored_description_is_refused_on_both_sides` |

`changeJobStatus` is `POST /tracker/{selector}/status`, `recordCompanyOutcome`
is `/outcome` and `rescoreJob` is `/rescore`. Every test named in the table
is in `services/api/tests/test_ui_tracker.py`.

The other responses on tracker writes:

- **`addJob` (`POST /tracker`) refuses nothing with a status code.** A
  duplicate or an invalid add is a 200 `AddJobOut` with `status` and
  `message`, a choice spec 042 made
  (`test_adding_the_same_job_twice_reports_the_duplicate`,
  `test_adding_without_a_title_is_refused`).
- **403** on all four writes: no token, or the wrong one (spec 035). The body
  is `{"detail": str}`, declared with a description only in
  `localauth.TOKEN_RESPONSES`. 29 operations declare a 403 with no body, four
  of them tracker writes
  (`test_a_tracker_write_without_the_token_is_refused`, status code only).
- **422**: the request fails the schema. `HTTPValidationError`, already typed
  (`test_a_company_code_is_not_a_rejection_code`,
  `test_company_outcome_route`).
- **503**: a host process holds the database (spec 075). `DatabaseHeldOut`,
  already typed
  (`services/api/tests/test_host_lease.py::test_every_route_that_opens_the_database_declares_the_503`).

Two details the table depends on:

- The 404's description, "no job matched the selector", is wrong for an
  ambiguous selector, which matched more than one. The browser always sends
  the numeric id, so from the page a 404 is always "no job with id N".
- `change_status` checks the verb before the selector, so an unknown verb on
  an id with no row is a 409, not a 404.

## Scope

- `services/api/src/harrier_api/app.py`:
  - A model, `ErrorOut { detail: str }`: the body of a refusal.
  - `TRACKER_ERRORS` declares `ErrorOut` for 404 and 409. The three routes
    above already use it, so each gains the body.
  - The 404's description becomes "the selector named no job, or more than
    one", which is what `SelectorError`'s docstring says the error means.
- `packages/contract/openapi.json` and `packages/contract/src/schema.d.ts`,
  regenerated with `just contract`. This is a guarded path, and this spec
  covers it.
- `apps/web/src/features/tracker/JobActions.tsx`: `refusalMessage` takes the
  generated types, and is exported so a test can pin its parameter.
- Tests in `services/api/tests/test_ui_tracker.py` and
  `apps/web/src/pages/tracker/TrackerPage.test.tsx`.
- `specs/042-the-ui-drives-the-tool.md`, amended in the implementing change
  to point here.

What any route sends does not change: no status, no body, no message. Nor do
`addJob`, the 403's declaration, the queue and count reads, or any route
outside `/tracker`.

## Behavior

### The body model

```python
class ErrorOut(BaseModel):
    detail: str
```

It describes the body FastAPI already sends for an `HTTPException`. The
routes keep raising `HTTPException(status_code=..., detail=str(error))`, so
every response is what it was. The model only declares it, and a test holds
the two together: a route that declares `ErrorOut` and sends something else
fails (Acceptance criteria).

`ErrorOut` sits in `app.py` beside `TRACKER_ERRORS`, as `ConfigErrorOut` sits
beside `CONFIG_ERRORS`. The name is general on purpose. The other routers'
refusals have the same body, and their own change can reuse it (Out of
scope). Moving the class to a shared module then is a refactor: the schema
keeps its name, so the contract does not change.

### What the contract declares

| Operation | 404 | 409 |
|---|---|---|
| `changeJobStatus` | `ErrorOut` | `ErrorOut` |
| `recordCompanyOutcome` | `ErrorOut` | `ErrorOut` |
| `rescoreJob` | `ErrorOut` | `ErrorOut` |

Each as `application/json`, a `$ref` to `#/components/schemas/ErrorOut`. The
404 reads "the selector named no job, or more than one". The 409 keeps "the
tracker refused the change".

In `schema.d.ts`, the six responses change from `content?: never` to an
`"application/json"` content of `components["schemas"]["ErrorOut"]`, each
404 gets the new description, and `ErrorOut` appears under `components`.
Nothing else in either file changes.

403, 422 and 503 are as they were. 422 and 503 already carry their models.
The 403 is open decision 1.

### How the browser reads a refusal

`refusalMessage` takes the error bodies the contract declares for these
operations, by their generated names:

```ts
type Refusal =
  | components["schemas"]["ErrorOut"] // 404, 409
  | components["schemas"]["DatabaseHeldOut"] // 503
  | components["schemas"]["HTTPValidationError"]; // 422
```

- It returns `detail` when that is a string (`ErrorOut`, `DatabaseHeldOut`),
  and the existing fallback, "the tracker refused that change", otherwise.
  `HTTPValidationError` carries a list of field errors, which means the
  browser sent a malformed request; the operator cannot act on it.
- No `unknown`, no cast, no hand-written shape.
- What the operator sees does not change for any status.

Two kinds of drift now fail `pnpm type-check`:

1. `ErrorOut` loses or renames `detail`: reading `detail` from the union no
   longer compiles.
2. One of the three operations declares a new error body: its call site can
   no longer pass `error` to `refusalMessage` until the reader accepts it.

The call sites keep both guards, `error !== undefined` and then
`data === undefined`. The second stays because the 403 still declares no
body, so the generated error type still includes `undefined`.

## Failure modes

- **A body FastAPI did not write.** An unhandled exception answers 500 with
  `text/plain` "Internal Server Error". openapi-fetch hands a body that is
  not JSON to the reader as a string, which has no `detail`, so the fallback
  shows, as today. The types do not describe this body. ADR-005 trusts the
  types at the seam and adds no runtime validation, and this spec keeps that.
- **An error with an empty body.** openapi-fetch returns neither `error` nor
  `data` when an error carries `Content-Length: 0`, and the
  `data === undefined` guard throws its existing message. Unchanged.
- **A new route under `/tracker/{selector}/` that does not declare its
  refusals.** The structural test fails until it declares `ErrorOut` for 404
  and 409 and has rows in the pairing table.
- **A route that declares `ErrorOut` and sends something else**, for example
  an `HTTPException` whose `detail` is a dict. The pairing test fails for
  each refusal it triggers. A refusal path it does not trigger is not seen
  (Honest limitations).
- **The contract regenerated, the browser not updated.** If `ErrorOut`
  changed, `pnpm type-check` fails at `refusalMessage`. That is the point.
- **The second run.** `just contract` is deterministic (ADR-005), so running
  it again leaves no diff. CI's `contract-drift` job checks that.

## Acceptance criteria

Tests marked planned do not exist yet. They are named here so the
implementation has a target; the implementing change cites each one in
backticks, where `tests/test_spec_structure.py` checks it exists.

The pairing table the first test runs:

| Operation | Refused with 404 | Refused with 409 |
|---|---|---|
| `changeJobStatus` | `{"verb": "shortlist"}` on an id with no row | `{"verb": "promote"}` on an existing row |
| `recordCompanyOutcome` | `{"code": "ghosted"}` on an id with no row | `{"code": "company_rejected"}` on a prospect |
| `rescoreJob` | an id with no row | a row with no stored description |

- [ ] For every operation and status in that table, the response has that
      status, a body that is exactly `{"detail": <non-empty string>}`, and
      the contract declares the status as `application/json` with `ErrorOut`
      (planned services/api/tests/test_ui_tracker.py::test_every_tracker_refusal_is_the_body_the_contract_declares).
      Without the change it fails: no body is declared, and `ErrorOut` does
      not exist.
- [ ] Every operation under `/tracker/{selector}/` declares 404 and 409 with
      `ErrorOut`; the 404 reads "the selector named no job, or more than
      one"; `ErrorOut` is an object whose one property, `detail`, is a
      required string; and the pairing table covers every such operation
      (planned test_every_tracker_write_that_names_a_job_declares_its_refusals,
      judged on `create_app().openapi()` the way
      `test_every_route_that_opens_the_database_declares_the_503` is).
      Without the change it fails on six declarations.
- [ ] `just contract` regenerates both artifacts, and their diff is what
      "What the contract declares" lists and nothing else (CI
      `contract-drift`, and the diff itself).
- [ ] `refusalMessage` takes `ErrorOut`, `DatabaseHeldOut` and
      `HTTPValidationError` by generated name, with no `unknown` and no
      cast. A type-level test pins its parameter to that union and each of
      the six bodies to `ErrorOut`, using vitest's `expectTypeOf`, which
      `pnpm type-check` compiles (`TrackerPage.test.tsx`: planned "a tracker
      refusal is read through the contract's types"). Without the change it
      does not compile.
- [ ] Shortlist, Company replied and Rescore each show a 404's and a 409's
      `detail` verbatim in the row's status line (`TrackerPage.test.tsx`:
      planned "each tracker write shows its refusal in the API's words").
      This passes before the change too. It pins that typing the reader did
      not change what it shows, and it is the browser's first proof of a
      rescore refusal and of any 404.
- [ ] The implementing pull request shows the two planned Python tests and
      the type-level test failing on main's code.
- [ ] Spec 042 is amended to point here: refusal bodies were not declared
      until this spec, so its "no hand-written response shape" criterion did
      not hold for them.
- [ ] `uv run ruff check`, `uv run pyright`, `pnpm type-check` and
      `pnpm lint` pass, and `just check` passes.
- [ ] No real tracker row appears in a fixture (ADR-008).

## Honest limitations

- **The operator sees no difference.** Every message shown before is shown
  after. What changes is that drift between the server and the browser fails
  a check instead of degrading to the fallback.
- **The declaration and the raise are still two things.** The routes raise
  `HTTPException`, and `TRACKER_ERRORS` declares the body. The pairing test
  holds them together for one refusal per route and status, not for every
  message the domain can raise.
- **The structural test sees only selector routes.** A tracker route outside
  `/tracker/{selector}/` that starts refusing is caught only if it declares
  the status. There is none today: `addJob` answers in its 200 body, and the
  queue and the counts are reads.
- **The 403 is still untyped on these routes.** It reaches the reader at
  runtime as `{"detail": ...}`, and the operator sees its words, but by type
  it is `undefined` (open decision 1).

## Out of scope

Each is recorded here for its own change.

- **The 403's body.** `localauth.TOKEN_RESPONSES` declares it with no model,
  and 29 operations carry it, four of them tracker writes (open decision 1).
- **The same declaration on other routers.** `OUTREACH_ERRORS` (404 and 409
  on 9 operations), `APPLY_ERRORS` and `ARTIFACT_ERRORS` (404 on 6), and
  `getRun`, `cancelRun` and `streamRunEvents`, which raise 404 and declare
  none. Their pages each carry a copy of the untyped `detail` reader:
  `apps/web/src/pages/apply/ApplyPage.tsx`,
  `apps/web/src/pages/inbox/InboxPage.tsx` and
  `apps/web/src/pages/outreach/OutreachPage.tsx`.
- **`ConfigErrorOut`** has `ErrorOut`'s shape. Folding the two renames a
  schema on the config routes, which is a contract change for them.
- **`AddJob.tsx` shows a 403, 422 or 503 as raw JSON**
  (`JSON.stringify(error)`).
- **The `data === undefined` guards name the token.** "the local API token
  was not accepted" is thrown only for an error with no body. A real 403
  carries a JSON body and is shown through the reader in the API's words.
- **Two refusals that would answer 500, not 409.** `rescoreJob` maps only
  `TrackerActionError` to 409, so a `TrackerError` from `update_fields`
  would be a 500. It is not reachable today, because a score writes none of
  the fields `harrier.tracker.invariants` reads. `IllegalTransitionError` is
  a `ValueError`, so a refused move would be a 500 on status and outcome.
  None is refused today
  (`test_every_status_is_reachable_from_every_wrong_one`).

## Open decisions for Akin

1. **The 403 on the tracker writes.** Proposed: leave it to its own change,
   with the other routers. Its declaration is shared by 29 operations, so
   typing it here means either a 403 declared one way on four of them and
   another way on the rest, or a contract change for every token-gated route
   and the narrowing at each of their web callers.
2. **The 404's description.** Proposed: corrected here, because this spec
   rewrites that declaration and the old text is wrong for an ambiguous
   selector. The alternative is to leave it and record it.
3. **A type-level test as the browser's proof.** The browser's behavior does
   not change, so no rendered test can fail without this change. Proposed:
   vitest's `expectTypeOf`, compiled by `pnpm type-check`. A mismatch fails
   `tsc -b`; that was tried with a throwaway file while this spec was
   written. It would be the first type-level test in `apps/web`.

## Data and privacy

The contract gains a schema and a reworded description, and no data. The
bodies do not change. An ambiguous selector's 404 lists the rows it matched
(company, title, status), as the command line does; the browser never sends
an ambiguous selector.

## Migration

None. The operator sees the same messages. The OpenAPI change adds bodies to
responses that were already declared, so a client that ignored them keeps
working.

## Proof / origin

- `services/api/src/harrier_api/app.py`: `TRACKER_ERRORS`, with descriptions
  only, and the three routes raising `HTTPException(status_code=404 or 409,
  detail=str(error))`.
- `packages/contract/src/schema.d.ts`: `content?: never` on the 404 and 409
  of `changeJobStatus`, `recordCompanyOutcome` and `rescoreJob`.
- `apps/web/src/features/tracker/JobActions.tsx`: `refusalMessage(error:
  unknown)` and its cast.
- `app.py`: `ConfigErrorOut` and `CONFIG_ERRORS`, the precedent for declaring
  a `{detail: str}` body (review finding on PR #20).
- ADR-005: an invented field fails `tsc`. Spec 042: the criterion this
  corrects. Spec 080: the outcome route, which took the same declaration.
- A contract review of the merged spec 077 to 081 range.
