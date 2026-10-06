---
spec: 080
title: The browser records a company's response apart from the candidate's rejection
status: accepted
approved: yes
milestone: M8
depends: [042, 056, 079]
---

# Spec 080: The browser records a company's response apart from the candidate's rejection

## Problem

Spec 079 records every decision as an event with an actor and a reason code,
and it keeps a company's verdict out of the candidate's decisions. It
deliberately left the browser alone, so the browser still works the old way:

- **The Reject control mixes the two.** `FREQUENT_REASONS` in
  `apps/web/src/features/tracker/JobActions.tsx` puts `rejected by company`
  next to `hybrid` and `missing stack` (spec 056). One click records a
  company's verdict through the candidate's Reject control.
- **The API takes only free text.** `StatusChangeIn` in
  `services/api/src/harrier_api/app.py` has `verb` and `reason: str`. Under
  spec 079 the code is then inferred from the text, which is a fallback, not a
  way to record data.
- **Interviewing is a candidate button.** The `interviewing` verb sits among
  the candidate's verbs, although it records something the company did.
- **Nothing in the browser records a ghosting or a non-response.** The
  candidate types it as a rejection reason, or does not record it at all.

The browser is where most decisions are made, so until it sends codes and
keeps the two actors apart, spec 079's separation holds for the command line
and is guessed for everything else.

## Scope

- API (`services/api/src/harrier_api/app.py`):
  - `StatusChangeIn` gains `reason_code`, typed as `RejectionCode`: the
    candidate and system codes from `harrier.tracker.reasons` (spec 079).
    Company codes are not members, so a company code on a status change is a
    422 from the schema, not a runtime check.
  - A new route `POST /tracker/{selector}/outcome`, operation id
    `recordCompanyOutcome`, body `CompanyOutcomeIn { code: CompanyOutcomeCode,
    note: str | None }`, returning `JobOut`. It calls the same domain function
    as `harrier company-outcome`.
  - `JobOut` does not change: it already carries `applied_date` and
    `status`, which is all the browser needs to decide what to offer.
- Contract: `packages/contract/openapi.json` and
  `packages/contract/src/schema.d.ts` regenerated with `just contract`. Both
  enums become generated string unions. This is a guarded path and needs
  approval under this spec.
- Web (`apps/web/src/features/tracker/JobActions.tsx` and its CSS):
  - Exit pills send a code and keep their text; `other…` gains a native
    code select.
  - Applied and interviewing rows show **Company replied** and **Withdraw**.
  - `interviewing` leaves the candidate verbs and becomes a company outcome.
  - Both takeovers share focus and Escape handling.
- Tests under `services/api/tests/` and `apps/web/src/`.

The domain behavior (events, actors, codes, inference, the refusal of a
company outcome on an unapplied row) is spec 079's and is not restated or
changed here. This spec only exposes it.

## Behavior

### The enums come from one table

`RejectionCode` and `CompanyOutcomeCode` are built from
`harrier.tracker.reasons` at import, not written out in `app.py`. A code added
to the table reaches the contract on the next `just contract` and becomes a
type error in the browser wherever a label for it is missing. The browser
never spells a code that the contract does not declare (ADR-005).

### Design principles

This is an Operate surface: a dense table the candidate works through many
times a day. The bar is earned familiarity, not a new idea per control. Four
principles decide everything below, and a reviewer can check each one:

1. **The word names who acted.** The candidate's exit and the company's
   response are different words on different controls, so habit cannot file
   one as the other. Separation is enforced by the schema, and the interface
   makes it the obvious path rather than a rule to remember.
2. **One mid-decision pattern, reused.** Spec 056's takeover (a button opens a
   row of one-click pills that replaces the verbs, with Cancel) is the only
   pattern for "close this with a reason". The company response uses the same
   pattern, the same pill anatomy and the same tokens. No new visual
   vocabulary, no modal, no separate styling.
3. **Color means consequence, not actor.** The danger hover marks a click that
   closes the row. It applies to every closing pill, the candidate's or the
   company's, and to none that keeps the row open (an interview invite).
4. **Width is a budget.** The actions column was narrowed once already to stop
   the table scrolling sideways (the comment in `JobActions.css`). Nothing here
   adds a control to the resting row, and the pill rows do not grow: rarer
   reasons go behind `other…`.

### The resting row by status

The resting row keeps its shape: one forward control, the exit control,
Apply where the page passes it, and More.

| Status | Forward control | Exit control |
|---|---|---|
| `prospect`, `shortlisted`, `tailored_cv_requested` | as today (Shortlist, Request CV, Applied) | **Reject** |
| `applied` | **Company replied** (replaces Interviewing) | **Withdraw** |
| `interviewing` | **Company replied** | **Withdraw** |
| `rejected` | Reopen (spec 072) | disabled, as today |

`Withdraw` and `Reject` call the same verb and record the same kind of event:
a candidate decision. Only the word changes, because after applying, the
candidate leaving and the company rejecting are the two things that happen
next, and they now sit side by side under names that cannot be confused.

`Interviewing` leaves `VERBS` and therefore leaves More as well. It is not
hidden; it moved to where it belongs.

### Exit pills (the candidate's decision)

Reject and Withdraw open the takeover with a label that matches the word
(`Reject:` or `Withdraw:`) and the frequent pills. Each pill sends a
`reason_code` and keeps its text:

| Pill | `reason_code` | `reason` (text kept on the row) |
|---|---|---|
| hybrid | `not_remote` | `hybrid` |
| onsite | `not_remote` | `onsite` |
| closed | `vacancy_closed` | `closed` |
| missing stack | `stack` | `missing stack` |
| location | `location` | `location` |
| language | `language` | `language` |
| other… | chosen in the select | the typed text |

- `rejected by company` is removed. This supersedes spec 056's pill list, and
  spec 056 is amended in the same change to point here. The six remaining
  pills keep the exact strings spec 056 stored, so new rows stay groupable
  with old ones.
- `closed` stays. It is the most common reason to close a prospect and a fact
  the candidate observes; its event actor is `system` (spec 079), which the
  code carries without the candidate having to think about it.
- `hybrid` and `onsite` share a code and keep their text.
- On a Withdraw takeover the location pills (`hybrid`, `onsite`, `location`)
  still apply: a posting that turned out not to be remote after applying is
  the candidate's reason to leave.

**`other…`** opens the existing free-text input, now with a native `<select>`
before it listing every candidate code not already on a pill (too senior,
too junior, contract type, timezone, company, compensation, other), each
with a plain label. It defaults to `other`. Confirm sends the selected code
and the typed text. A native select because it is the platform's own
affordance for "one of a known list", costs no width at rest, and needs no
custom keyboard handling. Company codes are not in it; they are not members
of `RejectionCode`, so they cannot be.

### Company replied (the company's outcome)

`Company replied` opens the same takeover, labelled `Company:`, with outcome
pills in lowercase like every other pill:

| Pill | `code` | Offered on | Closes the row |
|---|---|---|---|
| interview | `interview_invited` | `applied` | no |
| rejected | `company_rejected` | `applied`, `interviewing` | yes |
| assessment failed | `assessment_failed` | `applied`, `interviewing` | yes |
| ghosted | `ghosted` | `applied`, `interviewing` | yes |
| no response | `no_response` | `applied` | yes |

- One click submits to `recordCompanyOutcome`. No note field: spec 056
  established that the pill is the confirmation, and a company response is
  rarely more than its code. The API still accepts `note` for the CLI.
- `interview` is listed first on applied rows because it is the outcome that
  moves the row forward. It has the neutral accent hover; the closing pills
  have the danger hover (principle 3).
- The resulting status (`interviewing` for an invite, `rejected` otherwise) is
  set by the domain, not by the browser.

### Focus and keys

Both takeovers, and the existing Reject takeover with them, behave the same
way:

- Opening a takeover moves focus to its first pill. Today the Reject button
  unmounts when the pills appear and focus is lost; the same defect would
  repeat in the new takeover, so the shared pattern fixes it once.
- Escape, or Cancel, closes the takeover and returns focus to the control
  that opened it. A refused write, from a takeover or from More, moves focus
  to the message that explains it instead (amended below).
- Each takeover is a `role="group"` named by its label (`Reject`, `Withdraw`,
  `Company response`), so a screen reader announces whose decision it is.

### Errors

- A company outcome on a row no company engaged with, as spec 079 defines
  engagement: 409 with spec 079's message,
  shown verbatim in the row's existing status line. The browser
  never offers the control there, so this is reached only by a stale page or
  a hand-written request; the row then refetches.
- An unknown or company code on a status change: 422 from the schema.
- A status change with no `reason_code` still works: free text is inferred as
  spec 079 says. The CLI and old clients keep working.

### Dependency on spec 079

This spec relies on one behavior spec 079 does not yet state: the existing
`interviewing` verb (CLI `harrier interviewing`, API verb `interviewing`)
must record a `company` outcome with code `interview_invited`, not a
candidate decision. Otherwise the button this spec moves would survive in
the API as a back door that files a company action under the candidate.
Spec 079 is amended to say so before either is approved.

## Failure modes

- A double click on a pill: pills are disabled while a mutation is in
  flight, as spec 056 already requires.
- A stale page offering Company response on a row that was reopened: 409,
  surfaced in the row's error line, and the row refetches.
- The contract and the browser out of step: `pnpm type-check` fails, which is
  the point of generated types.
- Must not introduce: a company code reachable from Reject or Withdraw; a
  candidate code reachable from Company replied; a hand-written code string
  in the browser; a new control on the resting row; a modal; a change to
  statuses or transitions.

## Acceptance criteria

- [x] `reason_code` on a status change is stored on the event; a company code
      is refused with 422; no code still infers from text
      (`services/api/tests/test_ui_tracker.py::test_status_change_carries_a_reason_code`,
      `test_a_company_code_is_not_a_rejection_code`,
      `test_a_status_change_without_a_code_still_infers`)
- [x] `recordCompanyOutcome` records a `company` outcome with the given code
      and note, moves the status as spec 079 says, and returns 409 on a row
      no company engaged with
      (`services/api/tests/test_ui_tracker.py::test_company_outcome_route`,
      `test_company_outcome_refuses_an_unapplied_row`)
- [x] Both enums equal the actor partitions of `harrier.tracker.reasons`
      (`services/api/tests/test_ui_tracker.py::test_api_enums_come_from_the_reason_table`)
- [x] The contract is regenerated and `just contract` leaves no diff
      (CI contract check)
- [x] Each exit pill sends its code and its exact text; there is no
      `rejected by company` pill; `other…` sends the code chosen in its
      select, defaulting to `other`, and the select lists no company code
      (`apps/web/src/pages/tracker/TrackerPage.test.tsx`: "each pill sends its code and text",
      "the exit controls offer no company verdict",
      "other sends the selected code")
- [x] Applied and interviewing rows show Company replied and Withdraw, other
      rows show Reject, and the resting row has no more controls than before
      (`TrackerPage.test.tsx`: "the exit word names who acted",
      "the resting row does not grow")
- [x] Company replied offers the pills for the row's status and calls
      `recordCompanyOutcome`; interview does not carry the danger class and
      the closing pills do
      (`TrackerPage.test.tsx`: "company replied submits the company outcome",
      "danger marks the pills that close the row")
- [x] Interviewing is no longer a candidate verb or a More item
      (`TrackerPage.test.tsx`: "interviewing is a company outcome, not a verb")
- [x] Opening any takeover focuses its first pill; Escape and Cancel close it
      and return focus to its opener
      (`TrackerPage.test.tsx`: "a takeover keeps keyboard focus")
- [x] A refused write moves focus to the message that explains it: a refused
      company response, which closes its takeover, a refused rejection, and
      a refused action from More (`TrackerPage.test.tsx`: "a refused company
      response takes focus to its reason", "a refusal keeps focus where a
      browser drops it")
- [x] A status change mounts a new control in the forward slot rather than
      relabelling the focused one (`TrackerPage.test.tsx`: "a status change
      mounts a new control instead of relabelling the focused one")
- [x] Every danger hover outranks the ordinary hover in the cascade, so it
      shows in a browser (`TrackerPage.test.tsx`: "a danger hover outranks the
      ordinary hover")
- [x] The reason form behind `other…` is still the exit takeover's group,
      named by its word (`TrackerPage.test.tsx`: "other… stays a group named
      by its exit word")
- [x] A code other than `other` confirms without words and sends its label
      (`TrackerPage.test.tsx`: "a chosen code needs no words")
- [x] Spec 056 is amended to point to this spec for the pill list, and its
      existing pill tests are updated rather than deleted
- [x] `pnpm type-check`, `pnpm lint`, `uv run ruff check`, `uv run pyright`
      pass
- [x] No real tracker row appears in a fixture (ADR-008)
- [ ] All gates green on PR

## Honest limitations

- **The candidate still has to click.** A ghosting nobody records stays an
  `applied` row forever. Spec 079 lists Gmail-watch proposing outcomes as
  separate work; this spec does not detect anything.
- **Rare reasons cost two steps.** A code behind `other…` needs the select and
  Confirm. That is the price of keeping the row narrow; if one of them turns
  out frequent, it earns a pill by amendment, the way spec 056's last three
  did.
- **Hybrid and onsite share a code.** The model in spec 077 sees one
  `not_remote` signal; the text keeps the difference for anyone who needs it.

## Out of scope

- Gmail-watch proposing company outcomes.
- Editing or deleting a recorded outcome (events are append-only, spec 079; a
  correction is a new event, and a browser control for that is separate).
- Showing a job's event history in the browser.
- Any change to the queue, digest, scoring or statuses.

## Proof / origin

- `apps/web/src/features/tracker/JobActions.tsx`: `FREQUENT_REASONS` with
  `rejected by company`, and the `interviewing` entry in the verb list.
- `services/api/src/harrier_api/app.py`: `StatusChangeIn` with free-text
  `reason` only.
- Spec 079: the event log, the actors and the reason table this exposes; its
  Out of scope names this spec.
- Spec 056: the pill control this changes. Spec 042: the browser calls the
  same domain functions as the CLI. ADR-005: the browser speaks only in
  generated types.

## Amendment (2026-10-06, during implementation)

What implementation found, each with the test that proves it:

- **Spec 072's recruiter path is kept, as Interview invite.** Spec 072 R2
  promises that a rejected row can move straight to `interviewing` from the
  browser, because a recruiter can write after a rejection. Taking
  `Interviewing` out of More, with Company replied offered only on applied
  rows, would have removed that path, and with it the case spec 079's
  amendment keeps legal: an invitation needs no application. So More carries
  one item, **Interview invite**, on every row that is not applied or
  interviewing. It records the company's `interview_invited` through
  `recordCompanyOutcome`, so the word still names who acted and the resting
  row does not change. Spec 072 is amended to match.
  `apps/web/src/pages/tracker/TrackerPage.test.tsx`: "a rejected row can
  move straight to interviewing", "every verb the CLI has is reachable on
  the page".
- **"No more controls than before" is the shape, not each row's old count.**
  An interviewing row used to show no forward control at all, because no
  candidate verb leads on from it. It now shows Company replied in that
  slot. The width budget is the shape the column was narrowed to: one
  forward control, the exit, Apply and More, at most four, and no pill until
  a takeover opens. "the resting row does not grow" holds every status to
  that.
- **A chosen code does not need words.** Confirm behind `other…` waits for
  text only when the code is `other`, which says nothing without it. Any
  other code with no text stores the code's own label as the row's reason,
  so the row still reads as one. "a chosen code needs no words".
- **The select sits on its own line above the input.** Side by side, the
  actions column squeezed the reason input to a sliver and truncated the
  select (found by checking the page in a browser, demo mode). On its own
  line the select shows its labels in full and the input keeps the width it
  had before the select arrived. `JobActions.css`.
- **Every rejection code has a label in the browser.** The labels are a
  `Record` over the generated `RejectionCode` union, so a code added to
  `harrier.tracker.reasons` fails `pnpm type-check` until it has one. The
  select still offers only the candidate's codes; the system's are labelled
  but not offered.
- **The enums are built at import, and type checkers see a stub.** Pyright
  cannot follow an enum built at runtime, so `app.py` shows it an empty
  `StrEnum` under `TYPE_CHECKING`, while pydantic, FastAPI and the OpenAPI
  document get the class built from the reason table.
  `services/api/tests/test_ui_tracker.py::test_api_enums_come_from_the_reason_table`.
- **The outcome route is held to spec 042's pairing and to the token**, like
  every other tracker write:
  `services/api/tests/test_ui_tracker.py::test_the_outcome_route_and_the_cli_call_the_same_function`
  and the outcome row in `::test_a_tracker_write_without_the_token_is_refused`.

## Amendment (2026-10-06, review of PR #122)

A refusal from the outcome route closes the company takeover, so the pill
that had focus unmounts. Focus went to nothing, because only Escape and
Cancel handed it back. It now returns to Company replied, as on Escape and
Cancel. A refused rejection from the exit pills keeps its takeover open, so
focus stays on the pill and needed no change. Proof: "a refused company
response hands focus back to its opener" in `TrackerPage.test.tsx`, which
fails without the fix. (Superseded by the last amendment: focus now goes to
the refusal's message, and the claim about the exit pills does not hold in
a browser.)

## Amendment (2026-10-06, review of the merged range)

A review of the merged spec 077 to 081 range found two defects in this
spec's browser code and one claim its test did not prove.

- **The danger hover never showed.** `.job-actions button:hover:not(:disabled)`
  outranks a bare class selector, so Reject, Withdraw and every closing pill
  hovered in the ordinary accent. Principle 3 held in the markup ("danger
  marks the pills that close the row" checks the class) and failed in the
  browser. Both danger rules are now scoped like the ordinary hover, the way
  `.job-actions button.job-actions__cancel` already was. jsdom applies rules
  in source order and ignores specificity, so no rendered test sees this.
  The proof reads the cascade from the stylesheet as parsed: "a danger hover
  outranks the ordinary hover", which fails without the fix. It imports the
  stylesheet with `?raw`, which vitest stubs to an empty string unless
  `apps/web/vitest.config.ts` lets it through, so that file gains one line.
  Checked in a browser too (demo mode): Reject and a closing pill now hover
  in the danger color.
- **`other…` dropped the group.** The reason form replaced the pills' named
  group with an unnamed span, so a screen reader lost whose decision it was
  part way through. It is now the same `role="group"`, named by the exit
  word. "other… stays a group named by its exit word".
- **A claim cited a test that could not fail.** "A chosen code does not need
  words" cited "other sends the selected code", which always types words.
  "a chosen code needs no words" now proves it; requiring text for every code,
  or dropping the label fallback, fails it.
- **Company replied on an invited row.** The browser offers Company replied
  on every `interviewing` row, including one a recruiter invited before
  anyone applied. Spec 079 refused every response there until its own
  amendment of the same date, which counts an `interviewing` row as engaged.
  The Errors bullet and its criterion now name that rule rather than the
  `applied_date` column.

Out of scope, recorded for its own change: two more rules in
`JobActions.css` lose to `.job-actions button` the same way.
`.job-actions__primary` (spec 047) never applies its border or weight, and
`.job-actions__pill-other` (spec 056) never applies its muted color.

## Amendment (2026-10-06, review of the fixes)

A review of the merged fixes above, partly in a real browser, found two
focus defects. Each fix carries a test that fails without it.

- **Focus could land on a control nobody chose.** A refused company response
  returned focus to Company replied, but the refusal also refetches the row,
  and on a stale page the row is no longer applied or interviewing. Company
  replied and the forward verb share a slot and were not keyed, so React
  relabelled the focused button: it read Reopen, and Enter sent a verb
  nobody chose. A refusal now moves focus to its message instead, which is
  there before and after the refetch. The slot's two controls are keyed
  apart, so a status change mounts a new control rather than relabelling a
  focused one. "a refused company response takes focus to its reason"
  (renamed from "a refused company response hands focus back to its
  opener"), "a status change mounts a new control instead of relabelling
  the focused one".
- **A disabled control drops focus.** While a write runs, its controls are
  disabled, and a browser moves focus off a disabled control to the page.
  So a refused rejection left focus on the page with the pills still open,
  and so did a refused action from More. The amendment above said neither
  needed handling, which held in jsdom only. Every refusal now moves focus
  to its message. The test blurs the control while the request is pending,
  as the browser does: "a refusal keeps focus where a browser drops it".

The Errors bullet now defers to spec 079 for what counts as engagement,
which a separate change widens to a company that has already responded.
