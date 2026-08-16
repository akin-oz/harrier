---
spec: 049
title: The Inbox page
status: accepted
approved: yes
milestone: M8
depends: [018, 035, 042, 047]
---

# Spec 049: The Inbox page

## Problem

Spec 042 phase 4. The Gmail watch classifies incoming mail into kinds, decides
which are actionable, notifies through Telegram, and appends each event to
`events.jsonl`. None of that is visible in the UI. The operator sees a Telegram
message, and to see the history or to re-run the watch they open a terminal.

There is a constraint on this page that the phase description in spec 042 did
not account for, and it decides what the page can be.

**The archived event does not contain the message.** `redact_event` in
`harrier/mail/watch.py` keeps a fixed field set, reduces the sender to its
domain, and drops the subject and the body summary entirely, on the stated
grounds that they are the other party's words. `append_event` writes only that
form. So the store holds the classification, the company and role it was
matched to, the timestamp, the sender's domain, and whether it was actionable.
It does not hold the subject, the body, or who wrote it.

Spec 042 described this page as "what the mail watch classified, and the action
each one implies". The store supports the second half exactly and the first
half only as far as the classification, not the message.

## Scope

### What the page shows, and the fork it rests on

**This spec specifies the page over the archived events as they exist.** A row
is a classification: its kind, the company and role the cascade matched, when
it arrived, the sender's domain, whether it is actionable, and the next action
`suggest_next_action` returns for that kind. That is a real page and it needs
no new personal data anywhere.

It is weaker than an inbox. The operator cannot read the message from it, and
for `request_info`, where the action is "reply with the requested information",
the specific request lives only in Gmail.

**The alternative, marked as an open question for approval.** The page could
fetch the message body live from Gmail when a row is opened, showing it without
storing it. That keeps the archive as it is and adds no committed personal
data, but it adds a live mailbox read behind an HTTP route and makes the page
depend on the Gmail token being valid at view time rather than at watch time.

The third option, widening `ARCHIVED_FIELDS` to store subjects and summaries,
is **rejected here rather than left open**. It reverses a deliberate decision
with a stated reason, and it would put the other party's words into a file on
disk permanently. If that is wanted it belongs in a spec that amends 018 and
argues the point directly.

Approval of this spec is approval of the archived-events page. The live-fetch
option is not built unless a reviewer says so, in which case it lands as an
amendment to this spec with its own criteria.

### Routes

| Route | CLI verb | Domain function | Shape |
|---|---|---|---|
| `GET /mail/events` | none | the events reader | request |
| `POST /mail/watch` | `gmail-watch` | `run_watch` | run |

`run_watch` polls Gmail, classifies, and may notify. It is a run: it reaches a
remote service, its duration depends on how much mail is waiting, and its
failures are the kind an operator needs to see rather than a spinner that
stops.

The watch takes a dry-run flag, which the CLI has and which the page exposes,
because a dry run is how an operator checks the classifier without sending
notifications.

`GET /mail/events` reads the archived events, newest first, with a limit. It
is a read of a redacted store and follows the phase-1 rule that reads do not
carry the token. This differs from spec 047's artifact reads, which do, and the
reason is the redaction: an artifact is the candidate's own resume, and an
archived event has already had every identifying field removed.

### What stays CLI-only

`gmail-oauth` stays a terminal command, which spec 042 already decided: it is a
browser consent flow that writes a token, and it belongs where the operator can
see the whole exchange. `gmail-migrate-state` is a one-shot migration, already
run. The Operations page lists both with their reasons, which is spec 050's
job, not this one's.

## Inputs, outputs, failure modes

- Inputs: HTTP requests from the local browser. A limit and a dry-run flag.
- Outputs: the archived events as stored, and a run whose effects are exactly
  the CLI's.

Failure modes that must reach the operator:

- **The Gmail token is missing, expired, or unauthorized.** This is the most
  likely failure on this page and the one with a specific fix. The run fails
  saying the token is the problem and naming `gmail-oauth` as the command that
  repairs it. A generic "watch failed" here costs the operator the most time.
- **The Gmail dependencies are not installed.** The CLI prints the exact `uv
  sync` line. The run carries the same line.
- **Notification delivery fails.** `WatchSummary.send_failure` records it. The
  watch itself succeeded and the page distinguishes the two: mail was
  classified, and the Telegram message did not go out.
- **No new actionable messages.** An outcome, not an error. The CLI prints
  `gmail_watch=no_new_actionable_messages` and the page says the same in
  words. An empty table must not read as a failed load.
- **The events file does not exist yet.** The watch has never run. The page
  says that, rather than showing an empty list that looks like a clean inbox.
- **The events file has been rotated.** `_rotate_events` keeps the most recent
  lines and drops the rest, so the page shows a window rather than a history,
  and says so.

Failure modes this must not introduce:

- Any widening of what is written to `events.jsonl`.
- An unredacted message body reaching a committed file, a fixture, or a test.
- A second classification implementation. The page renders what
  `classify_message` decided and re-decides nothing.
- Anything that replies to mail. The watch reads and classifies; the operator
  replies in their own mail client.

## Acceptance criteria

Proving symbols are named at implementation.

- [ ] `POST /mail/watch` and the `gmail-watch` verb call the same domain
      function, asserted in the shape spec 042 established
- [ ] the dry-run flag reaches `run_watch` and suppresses notification, proven
      by a test that asserts nothing was sent
- [ ] `GET /mail/events` returns only fields `redact_event` archives, proven by
      a test that asserts the response of a seeded store carries no subject,
      no body, and no full sender address
- [ ] a missing token fails the run with a message naming `gmail-oauth`
- [ ] a notification failure is reported as a delivery failure, with the
      classification still reported as successful
- [ ] zero actionable messages renders as a stated outcome, and a
      never-run watch renders as "the watch has not run", and the two are
      different strings
- [ ] a rotated event file is presented as a window rather than a full history
- [ ] no route can write to `events.jsonl` except through `run_watch`
- [ ] the generated client carries every new route and no hand-written request
      or response shape appears in `apps/web`
- [ ] no personal data enters a committed fixture, a test name, or a
      screenshot. Every seeded event is an invented company at an invented
      domain
- [ ] all gates green on PR

## Proof / origin

`ACTIONABLE_KINDS`, `redact_event`, `ARCHIVED_FIELDS`, `append_event`,
`_rotate_events` and `suggest_next_action` are all in
`services/api/src/harrier/mail/watch.py` and were read rather than assumed.
`WatchSummary` and its `send_failure` and `actionable_count` fields are in
`harrier/mail/run.py`. The `no_new_actionable_messages` string and the exit
clamp are in `_cmd_gmail_watch` in `services/api/src/harrier_cli/main.py`. The
decision that `gmail-oauth` stays CLI-only is spec 042's table.

## Out of scope

The Operations page, spec 050. Storing message subjects or bodies, which is
rejected above. Replying to mail, or any outbound message other than the
existing Telegram notification. Changing the classification cascade or its
kinds. Gmail OAuth in the browser.

## Migration

None. An operator whose `events.jsonl` predates this spec sees it unchanged;
the page reads the existing format and this spec writes nothing new to it.
