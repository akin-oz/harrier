---
spec: 042
title: The UI drives the tool, not just the tracker table
status: accepted
approved: yes
milestone: M7
depends: [026, 035]
---

# Spec 042: The UI drives the tool, not just the tracker table

## Problem

The CLI has thirty-two top-level commands, several with subcommands of their
own. The API has thirteen routes. The web app has one page, which lists jobs
and starts a discovery run.

So everything the tool actually does for a job search happens in a terminal:
shortlisting, tailoring a resume, drafting a letter, answering application
questions, evaluating an offer, staging outreach, reading classified mail,
reconsidering rejections, checking feed health. The web app can watch a
discovery run and read the tracker, and that is all.

The gap is not a missing page. It is that almost no domain operation has an
HTTP route at all, so there is nothing for a page to call.

| Area | CLI | API today |
|---|---|---|
| tracker mutations | shortlist, track, interviewing, applied, reject, add, reevaluate | none |
| artifacts | tailor, cover-letter, answers, evaluate | none |
| outreach | find-contacts, contacts, outreach, outreach-draft, backfill-posters | none |
| mail | gmail-watch | none |
| operations | digest, schedule, parity, export, profile, and backup once spec 030 lands | none |
| discovery and config | discover, config, reconsider, check-feeds | runs, config |

## The security precondition

This spec widens an **unauthenticated** local HTTP API from thirteen routes
to several times that, and the new ones reach LLM spend, paid scraping,
mailbox reads, and destructive tracker writes.

Today that API has no authentication, a state-changing capture route that
answers GET, and no trusted-host middleware, so a page on any origin can
reach it and DNS rebinding makes a remote page same-origin. Spec 035 fixes
those and is approved and unbuilt.

**Spec 035 ships before any route in this spec.** Not as a nicety: a
`POST /tracker/{id}/reject` or a route that spends money, reachable from any
web page open on the machine, is a materially worse position than the one the
board already rated a P1. The dependency is declared above rather than left
to sequencing luck.

## Scope

**One route per domain operation, generated into the client.** Every route
goes through the existing contract discipline: FastAPI declares it, the
OpenAPI document is regenerated, `packages/contract` produces the types, and
the frontend imports them. No hand-written request shapes, which is the rule
`contract-guardian` already enforces.

**The CLI and the API call the same function.** Neither reimplements the
other. Every CLI verb already delegates to a function in `harrier`; the route
calls that same function. A behaviour that differs between the two is a bug
in one of them, and a test asserts the pairing for every operation.

**Long operations are runs, not requests.** Tailoring a resume, drafting a
letter, and discovery all take seconds to minutes and can fail halfway. They
use the existing run machinery with its event stream, so the UI shows
progress and the failure lands somewhere the operator sees. Only fast reads
and small writes are plain request-response.

**Five pages, by what the operator is doing:**

- **Tracker.** The table that exists, plus every status transition, the
  manual add, rescoring, and the queue ordering `next` and `review` produce.
- **Apply.** For one job: tailor a resume, draft a letter, draft answers,
  see the artifacts, and see the honesty gate's refusal when it refuses.
- **Outreach.** Contacts, the due queue, drafts. Nothing sends, which is a
  product invariant and stays one.
- **Inbox.** What the mail watch classified, and the action each one implies.
- **Operations.** Discovery runs, feed health, reconsideration, the schedule
  and its last-success ages, config, backup.

**What stays CLI-only, and says so in the UI.** Not everything should be a
button:

| Command | Why not |
|---|---|
| `cutover` | One irreversible sitting that requires an attestation the operator makes deliberately. A button invites a mis-click that stops the old scheduler. |
| `restore` | Destructive by design: it overwrites the tracker, and the case for running it is one where the operator should be reading carefully. Not yet on main; it arrives with spec 030. |
| `gmail-oauth` | A browser consent flow that writes a token; it belongs where the operator can see the whole exchange. |
| `migrate-legacy`, `gmail-migrate-state` | One-shot migrations, already run. |
| `demo-run` | A test harness for the run machinery. |

The Operations page lists these with the command to run and the reason,
rather than omitting them silently. A UI that quietly covers less than it
appears to is the same defect class as a document that overclaims.

## Inputs, outputs, failure modes

- Inputs: HTTP requests from the local browser and the bookmarklet.
- Outputs: the same domain functions the CLI calls, and the same artifacts.
- Failure modes that must reach the operator rather than a console: an LLM
  provider refusing, a truth-gate refusal, a paid source declining on the
  cost gate, a PDF that fails validation. Each of these is a normal outcome
  the CLI prints today and a UI must not swallow.
- Failure mode this must not introduce: a second implementation. If a route
  reimplements what a CLI verb does, the two drift and the tests pass
  because each covers its own copy.
- Failure mode this must not introduce: an operation that is fast in the CLI
  and a run in the UI, or the reverse, without the difference being visible.
  Anything that takes longer than a request should takes a run.

## Phasing

One spec cannot land that many routes honestly. This one delivers the pattern
and the first page; each later page is its own spec, and each names the
routes it adds.

1. **This spec:** the route-to-domain-function pairing and its test, the run
   versus request rule, and the **Tracker** page complete: every status
   transition, manual add, rescore, and the queue orderings.
2. Apply page, and the artifact routes.
3. Outreach page.
4. Inbox page.
5. Operations page.

## Acceptance criteria

Proving symbols are named at implementation. Phase 1 is delivered; the
criteria it does not reach stay unchecked, and the phase they belong to is
named so an unticked box is a schedule rather than an omission.

- [x] every tracker mutation the CLI can perform has a route, and a test
      asserts route and CLI verb call the same domain function
      (`services/api/tests/test_ui_tracker.py::test_the_cli_and_the_api_call_the_same_function`,
      `::test_rescore_goes_through_the_same_function`,
      `::test_every_transition_has_a_route`)
- [x] an illegal status transition is refused with the same message in both
      (`::test_an_unknown_verb_is_refused`, `::test_the_same_refusal_reaches_the_cli`,
      `::test_a_reason_on_a_non_rejection_is_refused`). One mapping is what
      makes this hold, pinned by
      `::test_the_browser_cannot_invent_a_transition_the_cli_lacks`.
- [x] the manual add deduplicates and scores exactly as the CLI's does,
      proven by a test that runs both against the same input
      (`::test_adding_a_job_scores_and_returns_it`,
      `::test_adding_the_same_job_twice_reports_the_duplicate`,
      `::test_a_duplicate_without_a_url_still_carries_the_row_it_clashed_with`)
- [x] `next` and `review` orderings are available and match the CLI's
      (`::test_the_queue_matches_the_cli_ordering`,
      `::test_the_undecided_queue_is_narrower`, and
      `apps/web/src/pages/tracker/TrackerPage.test.tsx::the queue view renders the server's ranking rather than re-sorting it`)
- [x] the generated client carries every new route; no hand-written request
      or response shape appears in `apps/web` (enforced by the contract
      codegen drift gate in CI, which regenerates and fails on a diff)
- [x] a refused operation surfaces its reason in the UI, with one test per
      refusal class the tracker path can produce
      (`TrackerPage.test.tsx::a refusal is shown in the words the API used`,
      `::a duplicate is reported in the domain's words and the form keeps its input`,
      and the rescore refusal at
      `test_ui_tracker.py::test_a_job_with_no_stored_description_is_refused_on_both_sides`)
- [ ] the Operations page lists the CLI-only commands with their reasons,
      asserted by a test so the list cannot drift from reality (phase 5)
- [x] no new route is reachable before spec 035's protections are in place,
      asserted by a test that the tracker routes require them
      (`::test_a_tracker_write_without_the_token_is_refused`,
      `::test_the_queue_is_readable_without_the_token`, and
      `TrackerPage.test.tsx::every tracker write carries the token and no read does`)
- [x] no personal data enters a committed fixture, a test name, or a
      screenshot (ADR-008). Every fixture here is an invented company.
      Limitation: this is a property of the diff rather than something a test
      can assert, and the secret scan does not look for it.
- [x] All gates green on PR

Phases 2 to 4, the Apply, Outreach and Inbox pages, add no criteria of their
own here: each takes its own spec, and this list stays about the seam and the
Tracker page.

## Proof / origin

The counts above were taken from the subcommand list `harrier --help` prints
and from the route decorators in `services/api/src/harrier_api/`, both counted
rather than estimated. They describe main at the time of writing, with six
specs still in review; `backup`, `restore` and `verify-backup` arrive with
spec 030 and are not counted here.

The request comes from the operator: everything available in the CLI should
be available in the UI.

## Out of scope

The four pages after Tracker, each its own spec. Authentication design,
which is spec 035. Any change to what a domain function does: this spec
exposes existing behaviour and fixes none of it. Mobile layout. Sending
anything to an employer, which no part of this system does.
