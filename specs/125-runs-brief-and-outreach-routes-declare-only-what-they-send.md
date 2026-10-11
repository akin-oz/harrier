---
spec: 125
title: The runs, brief and outreach routes declare the refusals they send, and only those
status: proposed
approved: no
milestone: M8
depends: [006, 048, 082, 094, 095]
---

# Spec 125: The runs, brief and outreach routes declare the refusals they send, and only those

## Problem

Spec 082 made the tracker writes declare their refusals with `ErrorOut`. It
left the other routers to their own change and named them under Out of
scope. The local review of PR #255 found two gaps there, and PR #263 listed
them as out of its scope. This spec owns both.

**1. Three run routes send a 404 their contract does not declare.**
`getRun`, `cancelRun` and `streamRunEvents` in
`services/api/src/harrier_api/app.py` raise
`HTTPException(404, "run not found")` when the run manager holds no run with
that id. `packages/contract/openapi.json` declares no 404 for any of them,
so the generated client has no type for that answer.

**2. Two shared response sets declare causes some of their routes never
send.**

- `BRIEF_ERRORS` is shared by `GET` and `PUT /apply/{selector}/brief`.
  `getBrief` raises 404 and 409 and declares a 400 it never sends.
  `putBrief` raises 400 and 404 and declares the 409 "the stored brief no
  longer parses", which only a read can meet. Each 404 also claims "no
  brief stored for it", which `putBrief` cannot answer.
- `OUTREACH_ERRORS` in `services/api/src/harrier_api/outreach_routes.py`
  gives nine routes the 409 "the domain refused the change". Five of them
  never raise it: `findContacts`, `listCandidates`, `approveCandidate`,
  `rejectCandidate` and `draftOutreach`. It gives all nine the 404 cause
  "the candidate was never staged", which only `approveCandidate` and
  `rejectCandidate` can answer.

A declared cause a route never sends is the same defect spec 094's review
fixed for the track 409 (review of PR #239: five reads claimed a 409 they
never send). A reader of the contract, human or generated, plans for an
answer that cannot come, and cannot tell it from one that can.

## The refusals today

Read from the routes, then from `create_app().openapi()` on main at
`87c3926`. Only own causes are listed. The track causes the route class
adds (spec 094), the run conflict 409 (`RUN_CONFLICT_RESPONSES`), 403, 422
and 503 are unchanged by this spec and left out.

| Operation | Raises | Declares (own causes) | Not sent |
|---|---|---|---|
| `getRun` | 404 run not found | nothing | the 404 is missing |
| `cancelRun` | 404 run not found | nothing | the 404 is missing |
| `streamRunEvents` | 404 run not found | nothing | the 404 is missing |
| `getBrief` | 404 no job, or no brief; 409 the stored brief does not parse | 400, 404, 409 | 400 |
| `putBrief` | 400 the store refused the brief; 404 no job | 400, 404 (job or brief), 409 | 409; "no brief stored" on 404 |
| `findContacts` | 404 no job | 404 (job or candidate), 409 domain | 409 domain; candidate on 404 |
| `listCandidates` | 404 no job | 404 (job or candidate), 409 domain | 409 domain; candidate on 404 |
| `approveCandidate` | 404 no job, or no staged candidate | 404 (job or candidate), 409 domain | 409 domain |
| `rejectCandidate` | 404 no job, or no staged candidate | 404 (job or candidate), 409 domain | 409 domain |
| `setBestContact` | 404 no job; 409 the contact is not linked to the job | 404 (job or candidate), 409 domain | candidate on 404 |
| `markOutreachSent` | 404 no job; 409 an illegal outreach move | 404 (job or candidate), 409 domain | candidate on 404 |
| `markOutreachReplied` | 404 no job; 409 a write the row's invariants refuse | 404 (job or candidate), 409 domain | candidate on 404 |
| `snoozeOutreach` | 404 no job; 409 an unparseable date | 404 (job or candidate), 409 domain | candidate on 404 |
| `draftOutreach` | 404 no job | 404 (job or candidate), 409 domain | 409 domain; candidate on 404 |

Every 404 "no job" here is `SelectorError`, raised through `_job_row` in
`outreach_routes.py` or `_job_id_for` in `app.py`: the selector named no
row, or more than one.

`findContacts` and `draftOutreach` still answer 409 for a run conflict,
and every outreach and brief route can answer the track's 409. Those stay.

A scan of every route's own body, comparing the statuses it raises with
the statuses its contract declares, found no other route with this shape.
The scan did not follow helpers, so it is a lead and not a proof (Honest
limitations).

## Scope

- `services/api/src/harrier_api/app.py`:
  - `getRun`, `cancelRun` and `streamRunEvents` declare 404 with
    `ErrorOut` (`harrier_api.deps.ErrorOut`).
  - `BRIEF_ERRORS` is replaced by two sets, one per brief route, each
    listing only what that route sends.
- `services/api/src/harrier_api/outreach_routes.py`: `OUTREACH_ERRORS` is
  replaced by sets that list only what each route sends, each with
  `ErrorOut`.
- `packages/contract/openapi.json` and `packages/contract/src/schema.d.ts`,
  regenerated with `just contract`. This is a guarded path, and this spec
  covers it.
- Tests in `services/api/tests/test_runs.py`,
  `services/api/tests/test_ui_operations.py`,
  `services/api/tests/test_ui_outreach.py`, and the one assertion in
  `services/api/tests/test_api_tracks.py` that pins today's draft 409 text.
- `specs/082-tracker-refusals-are-in-the-contract.md`, amended in the
  implementing change so its Out of scope entry points here.

No route changes what it sends: no status, no body, no message. Only the
declarations change.

## Behavior

### What the contract declares

Each own cause below is declared as `application/json` with `ErrorOut`.
Where a route also takes its scope from `scope_for`, the route class joins
the track causes to the same status, as it does today (spec 094). Where a
route starts a run, the run conflict cause stays on its 409.

| Operation | 400 | 404 | 409 |
|---|---|---|---|
| `getRun` | none | the run id names no run this server holds | none |
| `cancelRun` | none | the run id names no run this server holds | none |
| `streamRunEvents` | none | the run id names no run this server holds | none |
| `getBrief` | none | the selector named no job, or more than one; or no brief is stored for the job | the stored brief no longer parses, in the store's words; saving a brief replaces it |
| `putBrief` | the store refused the brief, in its words | the selector named no job, or more than one | none of its own |
| `findContacts` | none | the selector named no job, or more than one | none of its own |
| `listCandidates` | none | the selector named no job, or more than one | none of its own |
| `approveCandidate` | none | the selector named no job, or more than one; or the candidate was never staged | none of its own |
| `rejectCandidate` | none | the selector named no job, or more than one; or the candidate was never staged | none of its own |
| `setBestContact` | none | the selector named no job, or more than one | the domain refused the change |
| `markOutreachSent` | none | the selector named no job, or more than one | the domain refused the change |
| `markOutreachReplied` | none | the selector named no job, or more than one | the domain refused the change |
| `snoozeOutreach` | none | the selector named no job, or more than one | the domain refused the change |
| `draftOutreach` | none | the selector named no job, or more than one | none of its own |

"None of its own" means the 409 carries only the track causes, and for
`findContacts` and `draftOutreach` also the run conflict cause. Whether
the track can refuse a given operation is spec 094's rule and is not
changed here.

"The selector named no job, or more than one" is spec 082's wording for
the same `SelectorError`. The outreach 404 said "no job matched the
selector", which is wrong for an ambiguous selector (open decision 2).

The three run 404s come from one in-memory map in the run manager
(`RunManager.get` in `services/api/src/harrier_api/runs.py`), so an id from
before a server restart is also a 404. The description says "this server
holds" for that reason.

In `schema.d.ts`, the three run operations gain a 404 with
`components["schemas"]["ErrorOut"]` content. The brief and outreach
operations keep `ErrorOut` on the statuses they keep, with the narrower
descriptions; `getBrief` loses its 400. Nothing else in either file
changes.

### The browser

No page reads a status this spec removes. `BriefPanel.tsx` treats a
`getBrief` 404 as "no brief yet" and passes any other error to
`refusalMessage`; `useRunStream.ts` and `RunPanel.tsx` stringify a run
error. Each keeps compiling against the regenerated types. If
`pnpm type-check` disagrees, the narrowing it names is fixed in the same
change and recorded as an amendment.

## Failure modes

- **A route declares a cause and nothing provokes it.** The pairing test
  fails for that operation: its declared own causes must equal the causes
  its table rows provoke.
- **A route starts sending a refusal it does not declare.** The pairing
  test fails only if a table row provokes it. A new raise with no row is
  not seen (Honest limitations).
- **A route sends a body other than `{"detail": str}`.** The pairing test
  fails on that row's body check.
- **A new route under `/outreach/{selector}/`, `/apply/{selector}/brief`
  or `/runs/{run_id}`.** The coverage check fails until the table has a
  row for it.
- **The second run.** `just contract` is deterministic (ADR-005), so a
  second run leaves no diff. CI's `contract-drift` job checks that.

## Acceptance criteria

The pairing table the tests run. Each row is provoked over `TestClient`
against the suite's scratch database:

| Operation | Status | Provoked by | Cause |
|---|---|---|---|
| `getRun` | 404 | an id no run has | the run id names no run this server holds |
| `cancelRun` | 404 | an id no run has | the run id names no run this server holds |
| `streamRunEvents` | 404 | an id no run has | the run id names no run this server holds |
| `getBrief` | 404 | an id with no row | the selector named no job, or more than one |
| `getBrief` | 404 | a row with no brief | no brief is stored for the job |
| `getBrief` | 409 | a row whose stored brief no longer parses | the stored brief no longer parses, in the store's words; saving a brief replaces it |
| `putBrief` | 400 | a body with an unknown key | the store refused the brief, in its words |
| `putBrief` | 404 | an id with no row | the selector named no job, or more than one |
| each of the nine `/outreach/{selector}/` operations | 404 | an id with no row | the selector named no job, or more than one |
| `approveCandidate` | 404 | a LinkedIn URL never staged for the row | the candidate was never staged |
| `rejectCandidate` | 404 | a LinkedIn URL never staged for the row | the candidate was never staged |
| `setBestContact` | 409 | a contact not linked to the row | the domain refused the change |
| `markOutreachSent` | 409 | a prospect row, whose invariants refuse an outreach claim | the domain refused the change |
| `markOutreachReplied` | 409 | a prospect row, whose invariants refuse an outreach claim | the domain refused the change |
| `snoozeOutreach` | 409 | an `until` that is not a date | the domain refused the change |

- [ ] For each run row, the response is that status with a body that is
      exactly `{"detail": "run not found"}`, and `create_app().openapi()`
      declares the status as `application/json` with `ErrorOut` and the
      row's cause (planned test_an_unknown_run_is_the_404_its_contract_declares in
      test_runs.py).
      On main it fails: no run operation declares a 404.
- [ ] For each brief row, the response has that status and a body that is
      exactly `{"detail": <non-empty string>}`; the status is declared with
      `ErrorOut` and the row's cause; and each brief operation's own
      declared causes, per status, are exactly the causes of its rows
      (planned test_every_brief_refusal_is_one_its_contract_names in
      test_ui_operations.py).
      On main it fails: `getBrief` declares a 400 and `putBrief` a 409
      that no row provokes.
- [ ] The same for each outreach row, and the table has rows for every
      operation under `/outreach/{selector}/` (planned test_every_outreach_refusal_is_one_its_contract_names in
      test_ui_outreach.py).
      On main it fails: five operations declare the domain 409 and seven
      declare the candidate 404 with no row to provoke them.
- [ ] In both pairing tests, "own causes" are a status's description split
      on the route class's join, minus the track causes and the run
      conflict cause. Those two are proved where they are defined
      (`services/api/tests/test_api_tracks.py::test_every_scoped_route_declares_the_track_refusal`
      for the track's).
- [ ] `test_every_scoped_route_declares_the_track_refusal` keeps checking
      that a route's own 409 cause sits beside the track's, on a route that
      still has one (`markOutreachSent`), since `draftOutreach` no longer
      does.
- [ ] `just contract` regenerates both artifacts, and their diff is what
      "What the contract declares" lists and nothing else (CI
      `contract-drift`, and the diff itself).
- [ ] The implementing pull request shows each new test failing on main's
      code (its description, and an amendment here).
- [ ] Spec 082's Out of scope entry for these routers points here.
- [ ] `uv run ruff check`, `uv run pyright`, `pnpm type-check` and
      `pnpm lint` pass, and `just check` passes.
- [ ] No real tracker row, contact or brief appears in a fixture
      (ADR-008). The new rows use the suites' existing invented companies
      and `linkedin.com/in/` URLs that name no one.

## Honest limitations

- **The operator sees no difference.** Every status and message is what
  it was. What changes is that the contract stops promising answers that
  cannot come, and starts naming the run 404 that does.
- **The declaration and the raise are still two lines.** The pairing tests
  hold them together for one provocation per cause, not for every message
  the domain can raise.
- **A new raise with no table row is not seen.** The completeness check
  runs from the declaration to the provocation. Nothing runs from the
  route body to the declaration, because a raise inside a helper is not
  visible to a test that reads routes.
- **The scan that found no other case read only route bodies.** A route
  whose helper raises a status its contract omits would not have shown up.
  The run conflict and the track refusals are raised that way, and both
  are declared by mechanism rather than by hand.
- **The 403 is still untyped**, as spec 082 left it.

## Out of scope

Each is recorded here for its own change.

- **The 403's body.** `localauth.TOKEN_RESPONSES` declares it with no
  model, on every token-gated route (spec 082, open decision 1).
- **The pages' untyped error readers.** `OutreachPage.tsx`, `ApplyPage.tsx`
  and `InboxPage.tsx` read `detail` without the generated types, and
  `useRunStream.ts` and `RunPanel.tsx` show a run error as
  `JSON.stringify(error)`. Typing them is spec 082's pattern applied to
  those pages, and changes what code they compile.
- **Sharper wording for the outreach domain 409.** "the domain refused the
  change" covers four different refusals. Naming each changes four
  descriptions without changing what is sent, and is not needed to make
  the contract true.
- **`APPLY_ERRORS` and `ARTIFACT_ERRORS`.** Not audited cause by cause.
  Their 404 is raised through `_job_id_for` and `_apply_params`, which the
  route body scan does not follow, so the scan says nothing about them.

## Open decisions for Akin

1. **Causes, not only statuses.** The task named statuses: the run 404s,
   the brief 400 and 409, and the outreach 409. This spec also narrows what
   each 404 describes, because `putBrief` and seven outreach routes describe
   a 404 cause they cannot send. Proposed: include it, since a cause is
   what the description declares and spec 094's review treated a phantom
   cause as the defect. The alternative is statuses only, with the 404
   wording recorded as out of scope.
2. **"The selector named no job, or more than one" for the outreach and
   brief 404s.** Proposed: yes, spec 082's text for the same
   `SelectorError`. The alternative keeps "no job matched the selector",
   which is wrong for an ambiguous selector.
3. **Where the tests live.** Proposed: beside each router's existing
   tests (`test_runs.py` for spec 006, `test_ui_operations.py` for spec
   095's brief routes, `test_ui_outreach.py` for spec 048). The
   alternative is one file for this spec, which separates each test from
   the routes it pairs.

## Data and privacy

The contract changes descriptions and adds three response declarations. No
data. The bodies do not change. A brief and a staged candidate are
personal; the tests use invented rows, invented briefs and LinkedIn URLs
that name no one.

## Migration

None. A client that handled a declared 400, 404 or 409 keeps working: the
routes send what they sent. A client generated from the new contract can
no longer branch on a status the route never sends.

## Proof / origin

- `services/api/src/harrier_api/app.py`: `get_run`, `cancel_run` and
  `stream_run_events` raising 404 with no `responses` entry for it;
  `BRIEF_ERRORS` used by both `get_brief` and `put_brief`.
- `services/api/src/harrier_api/outreach_routes.py`: `OUTREACH_ERRORS` on
  nine routes, and the raises in each.
- `packages/contract/openapi.json` on main at `87c3926`: the declarations
  in "The refusals today".
- Spec 082: `ErrorOut`, and the Out of scope entry this spec takes over.
  Spec 094: the route class that joins track causes, and the review of
  PR #239 that removed phantom track causes.
- The local review of PR #255; PR #263's Out of scope list.
