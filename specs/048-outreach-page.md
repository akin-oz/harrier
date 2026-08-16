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

Drafts are generated from a job row and shown as text to copy. They are
artifacts, and they are read back through spec 047's artifact route rather than
a second mechanism.

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

Proving symbols are named at implementation.

- [ ] every outreach CLI verb in the table has a route, and a test asserts the
      route and the verb call the same domain function
- [ ] no route writes a contact except the approval route, proven by a test
      that enumerates the outreach routes and asserts which of them can reach
      the contact write path
- [ ] approving a candidate that was never staged is refused with the same
      message on both sides, and writes no contact
- [ ] approving a candidate syncs the tracker, as the CLI does
- [ ] a draft-generation request whose contact identifier matches nothing is
      refused rather than drafted with empty fields
- [ ] no route sends anything, proven by a test asserting the outreach routes
      reach no send path
- [ ] a paid operation is marked as paid in the UI before it runs, and the cost
      gate's refusal reaches the page in its own words
- [ ] zero staged candidates renders as "none found" rather than an empty table
- [ ] every outreach write requires the token; the due queue and the contact
      list are reads and do not
- [ ] the generated client carries every new route and no hand-written request
      or response shape appears in `apps/web`
- [ ] no personal data enters a committed fixture, a test name, or a
      screenshot. Contacts are the highest-risk fixture content in this spec:
      every staged candidate in a test is an invented person at an invented
      company
- [ ] all gates green on PR

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
