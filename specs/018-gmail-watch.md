---
spec: 018
title: Gmail watch and classification
status: in-progress
approved: yes
milestone: M4
depends: [004]
---

# Spec 018: Gmail watch and classification

## Problem

Readonly Gmail polling with the rule cascade and Telegram alerts. The
watcher never sends, replies, labels, or modifies mail: the only scope
is gmail.readonly, and the only outbound side effect is the operator's
own Telegram channel.

## Scope

- Package harrier.mail:
  - classification: the rule cascade ported intact (ignored via the
    marketing/security token list, interview_invite patterns,
    assessment, application_confirmation unless a recruiter follow-up
    token is present, rejection, request_info, scheduling via tokens or
    scheduling-service links, recruiter_reply, else ignored with
    no_matching_category), per-kind priorities and suggested next
    actions, the actionable set, and the compact Telegram formats (the
    application-confirmation variant drops the next-action line)
  - message normalization: Gmail API payload to GmailMessage (decoded
    headers, first text/plain part, base64url body decoding, RFC 2822
    timestamp parsing with a UTC-now fallback)
  - tracker matching: infer_company_role over the database rows
    (company+title in text beats company alone; a sender-domain match
    with title-token overlap breaks ties; fallback derives company from
    the sender domain and role from a title regex); the matched job id
    rides in the event as tracker_row
  - state and log: seen-message dedupe state at
    data/gmail-watch/seen_messages.json capped at 5000 ids; the event
    log at data/gmail-watch/events.jsonl is the digest's input
    (spec 019)
  - the run: fetch with lookback and max-messages from env
    (GMAIL_POLL_LOOKBACK_DAYS default 7, GMAIL_POLL_MAX_MESSAGES
    default 25), classify unseen messages, log every event, notify only
    actionable kinds through harrier.notify; dry-run prints per-message
    debug lines (missing ids, seen skips with reason, kind,
    actionable) and the counters, and sends nothing
  - oauth: credentials loading with refresh-and-persist and a setup
    flow (InstalledAppFlow local server), both behind lazy imports of
    the optional google dependency group with actionable install errors
- CLI: harrier gmail-watch [--dry-run], harrier gmail-oauth
  [--client-secret-file ...] [--token-file ...], and harrier
  gmail-migrate-state --from-root PATH copying the old repo's seen
  state into the data directory

## Inputs, outputs, failure modes

- Inputs: env (GMAIL_ACCOUNT, GMAIL_OAUTH_CLIENT_SECRET_FILE,
  GMAIL_OAUTH_TOKEN_FILE, lookback and max), the OAuth token file, the
  tracker rows, and the seen state. Outputs: event log lines, updated
  seen state, and Telegram messages for actionable events only.
- Failure modes: missing env variables raise RuntimeError naming them;
  a missing or invalid token raises RuntimeError pointing at harrier
  gmail-oauth; missing Google dependencies raise RuntimeError with the
  install command; a Telegram send failure stops the run with its exit
  code (per the old behavior) after the event was already logged;
  malformed API payloads normalize to empty fields rather than raising.

## Stated changes from the old code

- The seen state and event log live under the data directory
  (never-in-git): data/gmail-watch/ replaces state/gmail-watch/ and the
  repo-root gmail_handler.log.
- Tracker matching runs over the database rows and the event carries
  the job id (the old csv row number stays only in the migration path).
- The Google client libraries are an optional dependency group (gmail),
  lazily imported like Playwright in spec 013.
- The run is a library function returning a summary plus printable
  lines, so the counters are tested without patching print.

## Acceptance criteria

- [x] Behavior pins ported from the old tests/test_gmail_watch.py:
      the classification cascade (interview invite with tracker match,
      rejection, marketing ignore with reason, recruiter follow-up
      beating application confirmation, both confirmation phrasings,
      security-alert ignore), Telegram formats (compact actionable,
      low-noise confirmation), API message normalization, and the run
      counters (unseen and ignored counts, actionable counts, missing
      message id reporting, seen-skip reporting)
      (services/api/tests/test_mail.py)
- [x] Dry-run prints per-message classification and sends nothing
      (test_dry_run_counts_and_classifies_without_sending: the send
      function raises if called)
- [ ] All gates green on PR

## Proof / origin

Old repo scripts/gmail_watch_lib.py, gmail_watch.py,
setup_gmail_oauth.py, tests/test_gmail_watch.py. Proving file:
services/api/tests/test_mail.py. Honest limitation: the OAuth setup
flow and live Gmail fetch are proven only for their failure paths
(missing env, missing token, missing dependencies); live API calls are
not exercised in tests.

## Out of scope

The daily digest (spec 019, which consumes the event log), any Gmail
scope beyond readonly, sending or labeling mail (never in scope), and
scheduling the watcher (spec 020).
