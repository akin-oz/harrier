---
spec: 048
title: The Outreach page
status: accepted
approved: yes
milestone: M8
depends: [016, 017, 035, 042, 047]
---

# Spec 048: The Outreach page

## Problem

Spec 042 phase 3. Contact discovery, the approval queue, the due queue and
draft generation are all terminal-only. The API has no outreach route at all.

The operator's loop today is: find a job in the browser, open a terminal to
find contacts, read a JSON blob, open the staged artifact to decide who is
real, approve one by pasting a LinkedIn URL, and open the terminal again to
draft a message. The decisions in that loop are judgement calls about people,
which is the part a table in a browser serves better than a JSON dump, and the
part currently furthest from one.

## Scope

### The invariant this page must not break

**Contact discovery stages candidates for approval; nothing writes contacts
directly.** It is a product invariant, and it is the reason this page exists in
the shape below rather than as a list with a "find contacts" button that
populates it.

Discovery writes a staged artifact. A human reads it and approves or rejects
each candidate. Only approval writes a contact. The page makes both steps
visible: staged candidates are shown as staged, with the fit score and the
evidence discovery recorded, and approving one is a deliberate act on a
specific row.

**Nothing sends.** `mark-sent` records that the operator sent a message
themselves. The page says so in those words, because a button labelled "send"
next to a generated draft is the one misreading this invariant cannot survive.

### Routes

| Route | CLI verb | Domain function | Shape |
|---|---|---|---|
| `POST /outreach/{selector}/find-contacts` | `find-contacts` | `find_contacts_for_job`, `find_best_contacts_for_job` | run |
| `GET /outreach/{selector}/candidates` | reads the staged artifact | the staging reader | request |
| `POST /outreach/{selector}/candidates/approve` | `contacts approve` | `approve_candidate`, then `sync_tracker_outreach` | request |
| `POST /outreach/{selector}/candidates/reject` | `contacts reject` | `update_candidate_review_status` | request |
| `POST /outreach/{selector}/best-contact` | `contacts set-best` | `set_best_contact_for_job` | request |
| `GET /outreach/contacts` | `contacts list` | `list_contacts` | request |
| `GET /outreach/due` | `outreach due` | `outreach_due_rows` | request |
| `POST /outreach/sync` | `outreach sync` | `sync_tracker_outreach` | request |
| `POST /outreach/{selector}/sent` | `outreach mark-sent` | `mark_job_outreach_sent` | request |
| `POST /outreach/{selector}/replied` | `outreach mark-replied` | `mark_job_outreach_replied` | request |
| `POST /outreach/{selector}/snooze` | `outreach snooze` | `snooze_job_outreach` | request |
| `POST /outreach/{selector}/draft` | `outreach-draft` | `generate_outreach`, `write_outreach_draft` | run |
| `POST /outreach/backfill-posters` | `backfill-posters` | `backfill_posters` | run |

Contact discovery and draft generation are runs: the first calls a paid
external service and the second calls an LLM. Everything else is a small write
against the tracker and answers as a request.

Draft generation carries operator free text (audience, tone) and so uses the
run-scoped input file spec 047 defines. Contact discovery carries only a job
selector and a limit.

**Amended during implementation: what travels in that file, and what the run
mechanism had to grow.** Two gaps appeared once these verbs were routed.

The first is about whose data it is. Spec 047 kept *operator* free text off
argv. A draft also carries the contact: a name, a title, and a LinkedIn URL
belonging to a real person who never chose to use this tool. That is the same
hazard for a stronger reason, so the contact travels in the file too, as JSON,
and `outreach-draft` gains an `--input-file` flag to read it. The existing
per-field flags are unchanged and still work.

The second is that spec 047's parameters could not express these verbs.
They allowed one job and one boolean. `find-contacts` takes a count,
`outreach-draft` takes a different boolean, and `backfill-posters` acts on
every LinkedIn row and takes no job at all. So a kind now declares the closed
sets of switches and numeric flags its verb accepts, and a job becomes
optional. The guarantee spec 047 asked for is unchanged and easier to state:
a flag name reaching argv came from one of those sets, and a value reaching
argv is an integer or a path this process chose. A kind with no job locks on
the empty target, which is the one-at-a-time behaviour discovery already had.

### Money

`find-contacts` reaches Hunter, which is paid. `backfill-posters` reaches the
job sources. A button that spends money is different from one that does not,
and the page marks it: the operation says it will call a paid service before it
runs, and the cost gate's refusal surfaces in the gate's own words rather than
as a failed run with a stack trace.

### The page

Three sections, in the order the operator works:

- **Due.** The queue `outreach due` produces, each row carrying its next action
  and its best contact. Marking sent, marking replied and snoozing act here.
- **Candidates.** Staged candidates awaiting a decision, per job. Approve and
  reject act here, and approving syncs the tracker as the CLI does.
- **Contacts.** What has been approved, which is the read `contacts list`
  gives.

Drafts are generated from a job row. **Amended during implementation: the
page does not display them yet, and this section overstated twice.**

The first sentence of the original said drafts are shown as text to copy. The
delivered page has Due, Candidates and Contacts, and no control that reaches
`POST /outreach/{selector}/draft`; the route and its tests exist, and no page
calls them. The second sentence said drafts are read back through spec 047's
artifact route, and that is not possible as written: `write_outreach_draft`
writes to `outreach_drafts_dir()`, and spec 047's artifact route resolves a
closed set of kinds that does not include an outreach draft. Reading one back
needs a new kind in that set, which is spec 047's surface to widen and not
this spec's.

So drafting from the page is deferred, with both halves named: a control that
starts the draft run, and an artifact kind that lets the result be read back.
Whichever spec takes it owns both.

## Inputs, outputs, failure modes

- Inputs: HTTP requests carrying a job selector, a LinkedIn URL identifying a
  staged candidate, and for drafts the operator's audience and tone.
- Outputs: the same staged artifacts, contact rows and draft files the CLI
  produces, written by the same functions.

Failure modes that must reach the operator:

- **The candidate is not in the staged artifact.** Approving or rejecting a
  candidate that discovery never staged is refused with the CLI's message
  rather than silently creating a contact, which would break the staging
  invariant through the back door.
- **The contact is not linked to this job.** `set_best_contact_for_job` returns
  nothing, and the page says so rather than reporting success.
- **A supplied contact identifier matches no stored contact.** The CLI refuses
  rather than continuing with empty fields, after a review finding that it once
  did not. The route refuses identically.
- **Hunter declines, is rate limited, or the cost gate refuses.** The run fails
  carrying the service's own reason.
- **Discovery finds nobody.** Zero candidates is an outcome, not an error. The
  page says nobody was found rather than showing an empty table that reads like
  a loading state.

Failure modes this must not introduce:

- A path from the browser to a written contact that does not pass through
  approval.
- Anything that sends a message.
- A second implementation of the outreach state machine. The `outreach_status`
  axis is orthogonal to the tracker status lifecycle and stays that way.

## Acceptance criteria

All symbols below are in `services/api/tests/test_ui_outreach.py` unless
another file is named.

- [x] every outreach CLI verb in the table has a route
      (`::test_every_outreach_verb_in_the_spec_has_a_route`), and a test
      asserts the route and the verb call the same domain function
      (`::test_the_route_argv_reaches_the_same_function_the_cli_verb_does`,
      which executes the argv the route builds rather than patching a shared
      function, because a run *is* the CLI). Coverage of the whole run
      registry is held once, by
      `::test_every_parameterized_kind_is_reachable_from_a_page`
- [x] no route writes a contact except the approval route, proven by a test
      that enumerates the outreach routes and asserts which of them can reach
      the contact write path (`::test_only_the_approval_route_can_write_a_contact`)
- [x] approving a candidate that was never staged is refused with the same
      message on both sides, and writes no contact
      (`::test_approving_a_candidate_that_was_never_staged_is_refused`,
      `::test_approving_an_unstaged_candidate_beside_a_staged_one_is_refused`
      for the harder case where an artifact exists but omits this person, and
      `::test_the_cli_refuses_the_same_candidate_with_the_same_words`)
- [x] approving a candidate syncs the tracker, as the CLI does
      (`::test_approving_syncs_the_tracker_as_the_cli_does`)
- [x] a draft-generation request whose contact identifier matches nothing is
      refused rather than drafted with empty fields
      (`::test_a_draft_for_an_unknown_contact_is_refused_rather_than_drafted_empty`,
      which drives the argv the route builds so the refusal is proved to
      survive the trip through the input file)
- [x] no route sends anything
      (`::test_marking_sent_sends_nothing`, which patches
      `send_telegram_message` by its real name, paired with
      `::test_no_outreach_route_reaches_a_send_path` over the module's
      imports). The source-level half is a last resort by this repo's own
      rule, which is why it is a pair rather than the whole check
- [x] a paid operation is marked as paid in the UI before it runs
      (`apps/web/src/pages/outreach/OutreachPage.test.tsx::contact discovery
      is marked as reaching a paid service`), and a refusal reaches the page
      in its own words (`::a refused approval is shown in the words the API
      used`). Limitation: the cost gate's *own* refusal is not exercised
      end to end here, because it arrives as a failed run and this spec adds
      no run-log surface of its own; it is shown by the same mechanism spec
      047 built and tested
- [x] zero staged candidates renders as "none found" rather than an empty
      table (`OutreachPage.test.tsx::no candidates reads as none found rather
      than an empty table`, and `::test_no_staged_candidates_is_an_empty_list_not_an_error`
      for the API half)
- [x] **amended: every outreach read requires the token too, not only the
      writes.** This criterion originally said the due queue and the contact
      list are reads and therefore carry no token. Implementation showed that
      wrong. Those responses name a real human being who is not the operator
      and never chose to use this tool, which is a stronger case than the
      artifacts spec 047 already decided to authenticate. So every outreach
      route authenticates, reads included
      (`::test_outreach_reads_require_the_token`,
      `::test_outreach_writes_require_the_token`, and
      `OutreachPage.test.tsx::outreach reads carry the token`). Tracker reads
      are unchanged and still carry none
- [x] the generated client carries every new route and no hand-written request
      or response shape appears in `apps/web`: the page's rows are
      `components["schemas"]["OutreachRowOut"]`, `["CandidateOut"]` and
      `["ContactOut"]`, and the contract drift gate enforces the rest
- [x] no personal data enters a committed fixture, a test name, or a
      screenshot. Contacts are the highest-risk fixture content in this spec:
      every staged candidate in a test is an invented person at an invented
      company. Limitation: this is a property of the diff, and no test
      asserts it
- [x] all gates green on PR (`just check` passes: 1043 Python tests, 41 web
      tests, contract regenerated with no change to any existing route)

## Proof / origin

The verb inventory is spec 042's table. The domain functions are the imports in
`_cmd_find_contacts`, `_cmd_contacts`, `_cmd_outreach`, `_cmd_outreach_draft`
and `_cmd_backfill_posters` in `services/api/src/harrier_cli/main.py`. The
staging invariant and the no-send invariant are product invariants in
`CLAUDE.md`, carried from the old repo's OPERATIONS.md. The refusal on an
unknown contact identifier is a review finding already recorded in a comment in
`_cmd_outreach_draft`.

## Out of scope

The Inbox and Operations pages, specs 049 and 050. Any change to the outreach
state machine or to what a domain function does. Sending anything, ever.
Editing a draft in the browser. Bulk approval across jobs: approval is
per-candidate by design, and a bulk control is the shape that erodes it.

## Migration

None.
