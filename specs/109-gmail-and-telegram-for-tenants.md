---
spec: 109
title: Each owner brings a Telegram bot, and hosted Gmail waits for its own spec
status: accepted
approved: yes
milestone: M10
depends: [018, 040, 102, 104, 106, 112, 116]
---

# Spec 109: Each owner brings a Telegram bot, and hosted Gmail waits for its own spec

Akin approved the stub in chat on 2026-10-10; the frontmatter flip reached main inside PR #222, whose message does not say so, and this line records it. Refined 2026-10-10 after approval of the stub. This text is an amendment
to the approved stub and goes to Akin before implementation.

## Problem

Gmail watch and Telegram notifications are configured for one person
through local files and `.env`. Hosted tenants need their own. Google's
restricted Gmail scopes need app verification before external users can
grant them (ADR-013, Honest limitations).

The post-merge review of PRs #194 and #207 (architecture 9, 2026-10-10)
narrowed the choice. Gmail watch asks for one scope, `gmail.readonly`
(`services/api/src/harrier/mail/watch.py:36`), which Google classes as
restricted: an app that offers it to other people needs verification, and
a security assessment when it stores the data on servers (Google's Gmail
API scopes page,
https://developers.google.com/workspace/gmail/api/auth/scopes). A hosted
watch runs on Fly and writes to Supabase, so this spec must say whether
that counts as storing the data on servers. Either each tenant brings
their own Google OAuth client, and grants the scope to their own app, or
Gmail stays out of the first hosted release.

This spec answers both questions. It counts as storing and transmitting
(Behavior, "Gmail in the first hosted release"), and Gmail stays out of
the first hosted release. Telegram is offered: each owner brings a bot.

### Telegram today

- One bot and one chat for the whole install. `send_telegram_message`
  takes the token and chat id from its arguments, else from
  `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
  (`services/api/src/harrier/notify.py:32-33`). Those two lines are the
  only reads of either variable under `services/api/src` (`grep -rn
  'getenv("TELEGRAM' services/api/src`). Without either it logs a warning
  and returns 2 (`notify.py:34-39`).
- Every caller relies on that fallback. `send_telegram_message` is called
  at three sites (`services/api/src/harrier/discovery.py:481`,
  `services/api/src/harrier/academic/discovery.py:260`, `:349`) and is the
  default sender at two more (`services/api/src/harrier/digest.py:341`,
  `services/api/src/harrier/mail/run.py:68`). Counted with `grep -rn
  send_telegram_message services/api/src`, leaving out the import lines and
  the definition. None of the five passes a token.
- Every send happens inside a CLI process. No module under
  `services/api/src/harrier_api` imports `harrier.notify` (same grep). So
  a hosted send happens in a run, and a run's environment is spec 106's to
  build from its owner's credentials.
- A failed send is logged with the exception text, unscrubbed
  (`notify.py:57-58`). `scrub_secrets` exists for exactly this shape: its
  docstring names `http.client.InvalidURL`, whose message embeds the
  request path, and the path here is `/bot<token>/sendMessage`
  (`services/api/src/harrier/sources/__init__.py:27-43`). Hosted, logs
  leave the machine (spec 110, privacy 5).
- Demo mode never sends (`notify.py:25-31`,
  `services/api/tests/test_demo.py::test_demo_never_sends_telegram_even_with_credentials_present`).

### Gmail today

- The watch reads `GMAIL_ACCOUNT`, `GMAIL_OAUTH_CLIENT_SECRET_FILE` and
  `GMAIL_OAUTH_TOKEN_FILE` from the environment (`watch.py:595-602`).
  The token is a file on disk. A refresh writes the new token back to that
  file (`watch.py:643-645`).
- The grant is obtained by `harrier gmail-oauth`, which runs Google's
  installed-app flow with a browser and a loopback server on the operator's
  machine (`services/api/src/harrier_cli/main.py:914-953`). Inside the
  container `secrets/` is read only, so the CLI refreshes the token on the
  host before it delegates a watch (`main.py:3012-3030`).
- Its state is two files in the data directory: `seen_messages.json` and
  `events.jsonl` under `gmail-watch/` (`watch.py:163-172`). Spec 040 made
  the first written atomically and refused when damaged (`watch.py:448-460`)
  and the second redacted and bounded (`watch.py:472-523`). The digest
  reads the second (`digest.py:171-172`), and `GET /mail/events` serves it
  without a token (`services/api/src/harrier_api/mail_routes.py:93-108`).
- `POST /mail/watch` starts a run of kind `gmail-watch`
  (`mail_routes.py:111-125`, `services/api/src/harrier_api/runs.py:117-121`).
- The schedule runs the watch every 300 seconds (`config/schedule.json`,
  job `gmail-watch`), and the digest's schedule health names the
  `mail-watch` job among the three it always lists
  (`digest.py:290`, `services/api/src/harrier/runoutcome.py:40`).

Every one of these is one person's: one token file, one data directory,
one events file. Hosted, a data directory belongs to a Fly machine, not to
an owner. Spec 107 moves files to per-owner storage; nothing in it yet
covers a mailbox grant.

### What Google requires

- `gmail.readonly` is restricted. So are `gmail.metadata` and every other
  Gmail scope that reads messages; `gmail.labels` is the only Gmail scope
  listed as non-sensitive (scopes page). The classifier reads subject and
  body (`watch.py:373-376`), so no narrower scope serves it.
- A restricted scope needs restricted scope verification, and an app that
  accesses restricted data "from or through a third-party server" needs an
  annual security assessment by an independent assessor that Google
  approves (restricted scope verification page). The scopes page says it
  plainly: storing restricted scope data on servers, or transmitting it,
  needs a security assessment.
- Verification is not required for an app in development or testing, or
  for personal use. Google words the personal use exemption two ways: "if
  the app is for your personal use (fewer than 100 users)" (verification
  exemptions page) and "used by only a few users, all of whom are known
  personally to you" (restricted scope verification page).
- An app in Testing status, with an external user type, gets refresh
  tokens that expire in 7 days unless it asks only for the name, email and
  profile scopes (Google's OAuth 2.0 overview). A Gmail watch on such a
  client stops once a week until the person grants it again.
- A refresh token with Gmail scopes also stops working when the user
  changes their password, after six months unused, when the user revokes
  it, and when the account holds more than 100 refresh tokens for one
  client (OAuth 2.0 overview).

## Scope

1. **Telegram per owner, hosted.** Three routes let a signed-in owner
   save, read and clear their own bot token and chat id
   (`/notifications/telegram`, Behavior). They are stored with spec 116's
   per-owner credential store and reach only that owner's runs, through
   spec 106's run environment, as
   `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, so `notify.py` and its five
   callers do not change. The routes live in a new module,
   `services/api/src/harrier_api/notify_routes.py`.
2. **A Telegram check in `harrier.notify`.** A new function,
   `check_telegram(token, chat_id)`, used only by the save route: it checks
   the shapes, calls the Bot API's `getMe`, then sends one fixed
   confirmation message, and returns a reason from a closed set.
3. **A failed send logs no token.** `send_telegram_message` passes the
   exception text through `scrub_secrets` before logging it.
4. **Gmail is not offered hosted.** The hosted API answers 501 on both mail
   routes, the run manager refuses a hosted `gmail-watch` before it spawns,
   and the CLI's Postgres gate classes `gmail-watch` and `gmail-oauth` as
   local only. Spec 112's list of local-only commands takes the two, with
   an "Amended (spec 109)" note in spec 112 (Changes to approved specs).
5. **The digest says so on Postgres.** On a Postgres connection the digest
   opens no mail file, states that Gmail watch is not offered, and leaves
   `mail-watch` out of its schedule health.
6. **The hosted API holds no operator notification settings.** Start is
   refused while its environment carries `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_CHAT_ID` or any `GMAIL_` variable. This adds to spec 104's
   start-up refusals (Changes to approved specs).
7. **The contract.** The three Telegram routes, their models, and the 501
   on the two mail routes are declared. `packages/contract/openapi.json`
   and `packages/contract/src/schema.d.ts` are regenerated by `just
   contract`. A guarded path, approved under this spec.
8. **Documents.** ADR-013's Honest limitations line on Gmail gains
   "Decided (spec 109)" with the outcome. `docs/architecture.md`'s hosted
   section says Telegram is per owner and Gmail is local only, naming this
   spec. `.env.example` is not changed: its Telegram and Gmail lines
   describe the local product, which keeps them.
9. **Findings written into the specs that own them** (specs only, no code):
   - Spec 118, "A tenant connects Gmail with their own Google client", a
     stub created with this refinement, with `approved: no` and the
     headline under Behavior, "The Gmail follow-on".
   - Specs 108 and 114, approved: the lines under "Changes to approved
     specs".

Classification: `notify_routes.py` and the new test file sit under
`services/api/src/` and `services/api/tests/`, which
`config/data-classification.json` already classifies as public. The new
stub, spec 118, sits under `specs/`, also public. No new directory is
added.

**Size.** One pull request. Items 1 to 3 are the Telegram half, items 4
to 6 the Gmail half, and the diff touches `notify.py`, `digest.py`,
`runs.py`, `mail_routes.py`, `harrier_cli/main.py`, the new route module,
the contract and the documents. The Gmail connection itself, which would
double that, is split out (item 9) rather than built here.

## Behavior

### Telegram: what an owner does

An owner creates a bot with Telegram's @BotFather, sends their bot one
message so it may write to them, finds their chat id, and saves both in
harrier. From then on every notification their runs send (discovery
summaries, academic summaries, the digest) goes to their chat through
their bot. No other owner's run can reach it. Nothing else changes about
what is sent or when; "Nothing auto-sends" and "Telegram notifications are
the only outbound messages" stand as written.

### The routes

| Route | Hosted | Local |
|---|---|---|
| `GET /notifications/telegram` | the owner's state, no secret | 404 |
| `PUT /notifications/telegram` | check, then store | 404 |
| `DELETE /notifications/telegram` | remove both values | 404 |

Locally the routes are registered but answer 404 with `ErrorOut` detail
`Telegram is configured in .env on the local product`, so the exported
document is the same in both modes (spec 104,
`services/api/tests/test_hosted_auth.py::test_the_contract_is_the_same_in_both_modes`). Hosted, spec 104's
middleware requires a bearer token on each.

`TelegramSettingsIn`: `bot_token` (`SecretStr`), `chat_id` (`str`). Both
are plain strings to the schema, so a malformed value reaches the
handler's own check and is refused with a reason, never echoed back in a
validation error.

`TelegramSettingsOut`: `configured` (`bool`), `updated_at` (`str | None`).
It never carries the token or the chat id. A Telegram chat id identifies a
person, so it is treated like the token: stored encrypted, never returned,
never logged. Decision D7 (2026-10-11) makes this hold everywhere: spec
116 stores the chat id encrypted beside `bot_token` as a secret field, and
spec 106 scrubs it from run events and summaries.

### Saving

`PUT /notifications/telegram`, in order:

1. Shapes. The token must match `^\d+:[A-Za-z0-9_-]+$`, the form the Bot
   API documents. The chat id must match `^-?\d+$`. Channel usernames
   (`@name`) are not accepted.
2. `getMe` with the token. The Bot API describes it as "a simple method
   for testing your bot's authentication token".
3. `sendMessage` to the chat id with the fixed text `harrier: Telegram
   notifications are connected for this account.` It names nothing about
   the owner or their search.
4. Only when both calls succeed, both values are written through spec
   116's credential store for the request's owner, replacing any earlier
   pair, and the route answers 200 with `configured: true`.

The save runs in the request, with the values the request carried. The
API does not decrypt a stored credential for it, which keeps spec 116's
rule that stored credentials are decrypted only at run start (spec 106).
Each Bot API call has a timeout of 30 seconds, as `send_telegram_message`
uses (`notify.py:52`).

The confirmation is the one message harrier sends outside a run. It is a
Telegram message to the owner's own chat, caused by the owner's own
request, so it is inside the invariant.

### Refusals when saving

| Case | Status | `reason` | Stored |
|---|---|---|---|
| token not of the documented shape | 400 | `token_shape` | nothing |
| chat id not an integer | 400 | `chat_id_shape` | nothing |
| `getMe` answers `ok: false` | 400 | `token_rejected` | nothing |
| `sendMessage` answers `ok: false` | 400 | `chat_unreachable` | nothing |
| Telegram unreachable or times out | 502 | `telegram_unavailable` | nothing |
| demo mode | not reachable; spec 104 refuses demo mode hosted | | |

The body is `TelegramRefusalOut`: `detail`, a fixed sentence per reason,
and `reason`, the closed set above. The detail never quotes the token, the
chat id or Telegram's `description`. A refusal is logged with its reason
only. The mapping reads the Bot API's `ok` field, which the API documents,
not its `error_code`, which it says is "subject to change".

`chat_unreachable` covers the case where the owner has not yet written to
their bot. Its detail says to send the bot a message first and save again.

### Reading and clearing

`GET` answers `configured: true` and the time of the last save when both
values are stored for the owner, `configured: false` and `null` otherwise.
`DELETE` removes both and answers `configured: false`; deleting when
nothing is stored also answers that. Neither calls Telegram.

### A hosted run

A hosted run's environment is built by spec 106 from an allowlist. This
spec adds two entries: the owner's stored token as `TELEGRAM_BOT_TOKEN`
and chat id as `TELEGRAM_CHAT_ID`, both absent when the owner has none.
`send_telegram_message` then finds them as it does locally. An owner with
none gets today's missing-configuration result, 2, and the warning names
the variable, never a value. No `GMAIL_` variable is ever in the
allowlist.

### A failed send

`send_telegram_message` logs `telegram send failed: <text>` where `<text>`
is `scrub_secrets(str(exc))`, so a message that embeds `/bot<token>` logs
`/botREDACTED`. The return codes do not change.

### Gmail in the first hosted release

Not offered. The reasons, in order:

- **A hosted watch stores and transmits restricted data on servers.** It
  fetches full messages on a Fly machine (`watch.py:686`), keeps message
  ids as seen state, and archives derived events with the employer's
  domain, the matched company and role, and the message id
  (`watch.py:472-503`). Hosted, those would sit in Supabase. Google's
  scopes page asks for a security assessment when restricted data is
  stored on or transmitted through servers. Both happen, so the answer to
  the stub's question is yes.
- **One harrier app for everyone** would need restricted scope
  verification and an annual third-party security assessment. The routed
  finding already rules out a shared client asking for `gmail.readonly`.
- **Each tenant's own client** avoids a shared app but puts a Google Cloud
  project, a consent screen and an OAuth client on every tenant. Left in
  Testing status, its refresh tokens expire in 7 days and the watch stops
  weekly. Published unverified, it shows Google's unverified app screen and
  relies on the personal use exemption. Whether that exemption covers a
  tenant's app whose data passes through servers the tenant does not run,
  the operator's Fly and Supabase, is not stated in any page read for this
  spec. It is a policy question a staging run cannot answer.
- Nothing hosted keeps per-owner mail state yet. Spec 107 is not built.

So hosted Gmail is refused everywhere a hosted path could reach it:

- **API.** Hosted, `GET /mail/events` and `POST /mail/watch` answer 501
  with `ErrorOut` detail `Gmail watch is not offered on the hosted service
  (spec 109)`. Neither reads a file nor starts a run. Both declare the 501.
  Locally both behave as today.
- **Run manager.** Hosted, `RunManager.start` refuses kind `gmail-watch`
  with `GmailNotHosted` before it writes an input file or spawns anything,
  answered 501 with the same detail. This holds after spec 106 lifts spec
  104's refusal of every hosted run.
- **CLI on a Postgres URL.** `gmail-watch` and `gmail-oauth` move into
  spec 112's local-only class, with its text: `error: harrier gmail-watch
  is local only; it does not run while HARRIER_DATABASE_URL names a
  Postgres store`. The watch's state is a file in the data directory, which
  exists only beside a SQLite store. `gmail-migrate-state` is already in
  that class.

### The digest on Postgres

`build_digest` passes the connection's dialect (spec 112's
`Connection.dialect`) to the mail and health sections:

- On `"postgres"`, `actionable_updates` opens no file, and the section
  "Updates needing action" holds one line, `Gmail watch is not offered on
  the hosted service.`, instead of the `None` line it shows today when
  nothing matched. `None` would read as "no mail needs you", which is not
  what happened.
- On `"postgres"`, `schedule_health_lines` lists discovery, digest and the
  academic jobs and leaves out `mail-watch`, which can never succeed there.
  Spec 029's rule that a scheduled job is always named holds, because no
  hosted schedule runs it.
- On `"sqlite"`, both are exactly as today
  (`services/api/tests/test_digest.py::test_digest_renders_all_five_sections`).

### The hosted API's own environment

At start, hosted, the API refuses to start when its environment holds
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, or any variable whose name
begins `GMAIL_`, naming the variable and never its value: `error: the
hosted API must not hold <NAME>; notification settings are per owner
(spec 109)`, exit 1. `send_telegram_message` falls back to the
environment, so an operator's own bot in the API's environment would
receive every tenant's notifications if a run ever inherited it. Spec 106
builds run environments from an allowlist; this check makes the value
absent in the first place.

### The Gmail follow-on

Spec 118, the stub this spec creates, carries this headline, so the
findings are not lost:

> Each owner connects Gmail with an OAuth client in their own Google Cloud
> project, granting `gmail.readonly` to their own app, never to a shared
> harrier client. The client id, client secret and refresh token are spec
> 116 credentials, given to the watch's run by spec 106. The grant uses a
> web flow with a redirect to the hosted API, state and PKCE, not `harrier
> gmail-oauth`'s loopback flow. A
> refreshed access token stays in the run's memory and is never written
> back. An expired or revoked refresh token (Testing status after 7 days, a
> password change, six months unused, revocation, the 100-token limit per
> client) is reported to the owner as "reconnect Gmail", never retried in a
> loop. The seen state and the events archive move to the owner's prefix
> through spec 107's seam, keeping spec 040's rules: an absent object is a
> first run, a damaged one is refused, never read as empty, and the archive
> stays redacted and bounded. Spec 108 runs at most one watch per owner at
> a time, so two runs cannot overwrite each other's seen state. Before
> approval it records, on spec 110's staging project, whether a tenant
> client published unverified keeps its refresh token past 7 days, and it
> states what is known of the personal use exemption for a client whose
> data passes through the operator's servers.

## Failure modes

- **Wrong token.** `getMe` refuses it: 400 `token_rejected`, nothing
  stored, any earlier pair kept.
- **Right token, owner never wrote to the bot, or wrong chat id.** The
  confirmation fails: 400 `chat_unreachable`, nothing stored.
- **Telegram down or slow.** 502 `telegram_unavailable` after at most 30
  seconds per call, nothing stored. The owner saves again later.
- **Token revoked in @BotFather, or the owner blocks the bot, after
  saving.** Runs keep sending and fail: `send_telegram_message` returns 1
  and logs the scrubbed reason. Discovery still records its result
  (`discovery.py:484`, outside the send); the digest does not record success on a failed
  send (`digest.py:347-355`), so its health line ages, which is spec 029's
  signal. The owner saves a new pair.
- **Owner never saved Telegram.** Runs find no variables and get 2, as a
  local install without `.env` values does today
  (`services/api/tests/test_discovery.py::test_notify_returns_2_without_config`).
- **Owner B reads or clears owner A's settings.** Not possible through the
  routes: they act on the request's `sub` only, and the store is spec
  116's, under spec 105's policy.
- **A run gets another owner's Telegram values.** Spec 106's allowlist
  builds the environment from the run's owner only; this spec's test
  proves the two Telegram entries follow it.
- **A token in a log or a body.** Refusal bodies use fixed sentences; the
  request body's token is a `SecretStr`; a send failure is scrubbed.
- **Operator Telegram or Gmail variables on the Fly app.** Start refused
  (Behavior).
- **A hosted `gmail-watch` by any path.** 501 from the routes and the run
  manager; the CLI refusal on a Postgres URL if a process is started some
  other way. No file is read and no Google call is made.
- **A hosted digest.** Says Gmail is not offered and omits `mail-watch`
  from its health lines. Opens no file under the data directory for mail.
- **Two saves at once for one owner.** Each runs its own check and
  confirmation; the store keeps the last write. Both pairs passed the
  check, so either is valid.
- **Rate limits.** Telegram asks bots to send no more than about one
  message per second to one chat. Each owner has their own bot, so owners
  do not share a limit. Nothing here adds a send loop.

## Acceptance criteria

Telegram, hosted (Postgres tests with spec 104's hosted app and spec 116's
store; the Bot API is a fake injected into `check_telegram`, and no test
reaches the network):

- [ ] A save with a valid token and chat id calls `getMe`, then sends
      exactly one message with the fixed text, stores both values for the
      request's owner, and answers 200 `configured: true`
      (planned test_saving_telegram_checks_the_bot_and_sends_one_confirmation).
- [ ] Each refusal in the saving table answers its status and reason, and
      stores nothing, and an earlier pair survives a refused save
      (planned test_a_refused_telegram_setting_stores_nothing, one case per
      reason).
- [ ] No response body and no log record captured during the Telegram
      route tests contains the token or the chat id
      (planned test_a_telegram_secret_never_reaches_a_body_or_a_log).
- [ ] `GET` returns `configured` and `updated_at` and no other field;
      `DELETE` removes both values and answers `configured: false`
      (planned test_reading_and_clearing_telegram_settings).
- [ ] Owner B's `GET` after owner A's save answers `configured: false`, and
      B's `DELETE` leaves A's pair
      (planned test_telegram_settings_are_per_owner).
- [ ] A hosted run of owner A has A's `TELEGRAM_BOT_TOKEN` and
      `TELEGRAM_CHAT_ID`, a run of owner B with none saved has neither, and
      no run has a `GMAIL_` variable. Moved to spec 106 by its Amendment
      (2026-10-13, spec 109), which carries the planned test; see this
      spec's Amendment (2026-10-13).
- [ ] The hosted API refuses to start with `TELEGRAM_BOT_TOKEN`,
      `TELEGRAM_CHAT_ID` or a `GMAIL_` variable in its environment, naming
      the variable and printing no value, one case each
      (`services/api/tests/test_tenant_notifications.py::test_hosted_start_refuses_operator_notification_settings`).

Telegram, both modes:

- [ ] A send whose exception text holds `/bot<token>` logs
      `/botREDACTED` and returns 1
      (`services/api/tests/test_tenant_notifications.py::test_a_telegram_failure_log_never_holds_the_token`).
- [ ] Locally, the three Telegram routes answer 404 with the local detail,
      and `services/api/tests/test_demo.py::test_demo_never_sends_telegram_even_with_credentials_present`
      and `services/api/tests/test_mail.py::test_live_run_sends_actionable_and_stops_on_send_failure`
      pass unchanged (planned test_telegram_routes_are_not_served_locally).

Gmail:

- [ ] Hosted, `GET /mail/events` and `POST /mail/watch` answer 501 with the
      spec 109 detail, read no file and start no run
      (`services/api/tests/test_tenant_notifications.py::test_hosted_mail_routes_answer_501`).
- [ ] Hosted, `RunManager.start("gmail-watch", ...)` raises
      `GmailNotHosted` and writes no input file and spawns nothing, also
      with spec 104's run refusal lifted
      (`services/api/tests/test_tenant_notifications.py::test_a_hosted_gmail_watch_run_is_refused_before_it_spawns`).
- [ ] With a Postgres URL, `harrier gmail-watch` and `harrier gmail-oauth`
      exit 1 with the local-only text and open nothing
      (planned test_gmail_commands_are_local_only_on_postgres), and spec
      112's command walk still places every command in one class.
      Delivered by spec 127, whose Scope item 7 and criteria already hold
      both, and whose walk is its planned
      test_every_command_has_a_postgres_refusal_class; see this spec's
      Amendment (2026-10-13).
- [ ] On a Postgres connection the digest's "Updates needing action" holds
      the not-offered line, its health lines omit `mail-watch`, and no file
      under the data directory is opened; on SQLite the digest is unchanged
      (`services/api/tests/test_tenant_notifications.py::test_a_postgres_digest_reads_no_mail_file_and_says_why`).
- [ ] Locally, every test in `services/api/tests/test_mail.py`,
      `services/api/tests/test_mail_watch.py` and
      `services/api/tests/test_digest.py` passes unchanged.

Contract and documents:

- [ ] `packages/contract/openapi.json` declares the three Telegram routes
      with `TelegramSettingsIn`, `TelegramSettingsOut` and
      `TelegramRefusalOut`, and the 501 on both mail routes; `just
      contract` leaves no diff after the regenerated files are committed.
- [ ] ADR-013's Honest limitations line on Gmail carries "Decided (spec
      109)" with this outcome, and no other ADR-013 line changes.
- [ ] `docs/architecture.md`'s hosted section says Telegram is per owner
      and Gmail is local only, naming this spec and spec 118.
- [ ] Once approved, spec 112 carries an "Amended (spec 109)" note moving
      `gmail-watch` and `gmail-oauth` to its local-only class.
- [ ] Spec 118 exists with `approved: no` and the headline under
      Behavior; once approved, the specs named under "Changes to approved
      specs" carry those lines; and nothing else in them changes.
- [ ] Under `services/api/src`, the diff touches only the new
      `harrier_api/notify_routes.py`, `harrier/notify.py`,
      `harrier/digest.py`, `harrier_api/runs.py`,
      `harrier_api/mail_routes.py`, `harrier_cli/main.py`, the module that
      holds spec 104's start-up checks, and the module that holds spec
      106's run environment allowlist.

Gates:

- [ ] `uv run ruff check`, `uv run pyright`, `pnpm type-check` and
      `pnpm lint` pass.
- [ ] `just check` passes.

## Honest limitations

- **Hosted tenants get no mail watch.** The inbox, the mail notifications
  and the digest's mail section exist only in the local product until the
  follow-on lands. The hosted product is a discovery, tracking and
  drafting tool without them.
- **Google's policy for a tenant's own client is not settled here.** The
  pages read say what the exemptions are; none says how they apply when a
  tenant's app sends data through someone else's servers. The follow-on
  states what it can find; it may not be able to settle this without
  asking Google.
- **The confirmation proves the pair once.** A bot token revoked later, or
  a bot the owner blocks later, shows up only as failed sends and an
  ageing digest health line. No route reports it.
- **A Telegram bot token is full control of that bot.** Harrier holds it
  encrypted (spec 116) and only the owner's runs see it. A compromised run
  of that owner can read it, which spec 106's trust model already states
  for every credential.
- **Notification content goes to Telegram,** as it does locally: company,
  title, location, score and URL for discovery, and the digest's lines.
  Nothing new is added to it.
- **The shape checks are documented forms, not guarantees.** A token
  format Telegram changes later would be refused at save until the pattern
  changes.
- **Harrier never reads the chat.** It does not receive updates, so it
  cannot find the chat id for the owner. The owner finds it.
- The line numbers cited here are refreshed by the change that implements
  this spec, not before (decision D9, 2026-10-11).

## Migration

None for the local product: `.env`, `secrets/`, `harrier gmail-oauth`,
the watch, the digest and the inbox behave as today on SQLite.

Hosted: no hosted install exists. When one does, each owner starts with
no Telegram settings, so their runs notify nobody until they save a pair.
The operator sets no Telegram or Gmail variable on the Fly app; spec 110's
deploy must not either, or the API refuses to start.

## Changes to approved specs

Each line below is a change this spec asks of an approved spec. None is
written into that spec here; each is an amendment Akin approves with this
spec.

- **Spec 104:** hosted start-up also refuses `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHAT_ID` and any `GMAIL_` variable in the API's environment
  (Behavior, "The hosted API's own environment").
- **Spec 108:** the hosted schedule has no `gmail-watch` job until spec
  118, and a digest for an owner with no Telegram settings is reported as
  not configured, not as failed. Spec 108's refined text already leaves
  `gmail-watch` out of `HOSTED_JOBS` and records a missing credential as
  `not_configured`.
- **Spec 112:** its local-only class takes `gmail-watch` and `gmail-oauth`,
  with an "Amended (spec 109)" note; its
  planned test_every_command_has_a_postgres_refusal_class places them
  there.
- **Spec 114:** a settings form for the Telegram routes. Spec 114's
  refined text of 2026-10-10 does not carry it. Written into spec 114's
  Amendment (2026-10-11) (decision D5).

## Options weighed

**Gmail:**

- **(a) One harrier Google app, verified by the operator.** Every tenant
  would grant `gmail.readonly` to one client. It needs restricted scope
  verification and an annual security assessment by a Google-approved
  third party, because the data passes through and is stored on servers.
  The pages read do not state the assessor's cost. The routed finding
  rules out a shared client. Not chosen.
- **(b) Each tenant brings their own OAuth client, now.** No shared app.
  Each tenant creates a Google Cloud project and client and pastes its
  secret into harrier; in Testing status the watch dies every 7 days, and
  published unverified it rests on an exemption whose fit is unconfirmed.
  It also needs a web OAuth flow, per-owner mail state in Storage (spec
  107, not built) and per-owner scheduling (spec 108, not built). Not chosen for
  the first release; it is the follow-on's design.
- **(c) Gmail out of the first hosted release.** Chosen. The hosted
  product works without it, and a refusal can be stated and tested now.
- **(d) A narrower Gmail scope.** `gmail.metadata` is restricted too, and
  the classifier needs the body. Not available.
- **(e) Tenants forward job mail to an address harrier receives.** Needs an
  inbound mail service, which is not in the amended Local-first list
  (Supabase, Fly.io, Vercel). Not chosen without a spec changing that list.
- **(f) IMAP with an app password.** A password with full mailbox access,
  stored by harrier, instead of a revocable scoped grant. Not chosen.

**Telegram:**

- **(a) Each owner brings a bot.** ADR-013 decision 8 already lists
  "Telegram bot" as a tenant credential. No inbound surface, no shared
  limit, and a run carries only its owner's token. Chosen.
- **(b) One operator bot; owners link their chat.** The owner would press
  start on harrier's bot and harrier would learn the chat id. That needs
  harrier to receive updates (a webhook or polling), a new inbound surface;
  the operator's token would be needed in every run or a central sender;
  and every owner's messages would share one bot's limit. Not chosen.

**The save check:**

- **(a) `getMe`, then one confirmation message.** Proves the token and that
  the bot can write to the chat, in the request. Chosen.
- **(b) `getMe`, then `getChat`.** Sends nothing. Whether `getChat` on a
  private chat fails until the user has written to the bot is not stated in
  the parts of the Bot API page read for this spec. Not chosen.
- **(c) Shape checks only.** A wrong chat id would surface as a silent
  missing digest days later. Not chosen.

**Where hosted Gmail is refused:**

- **(a) API, run manager and CLI gate.** Each path into the watch has its
  own refusal, so a new route or a scheduled run cannot reach it. Chosen.
- **(b) The API routes only.** A scheduled or hand-started run on Postgres
  would still read a machine-wide file. Not chosen.

## Decisions taken (2026-10-11)

Akin took these in chat on 2026-10-11 ("go with your recommendations").
The text above says where each changed it.

- **D4, spec 110 takes this spec's asks.** The assumptions listed under
  Proof / origin for spec 110's staging check, the rule that no Telegram
  or Gmail variable is set on the Fly app (Migration), and a hosted image
  with no `gmail` group are written into spec 110's Amendment
  (2026-10-11).
- **D5, the Telegram settings form.** Spec 114 gains it by amendment
  (Changes to approved specs). This takes Open decision 5 as recommended.
- **D7, the Telegram chat id is secret everywhere.** This spec keeps
  treating it as secret (Behavior, "The routes"); spec 116 stores it
  encrypted beside `bot_token`, and spec 106 scrubs it from run events and
  summaries.
- **D9, line numbers.** Refreshed by this spec's implementing change
  (Honest limitations).

## Open decisions for Akin

1. **Gmail in the first hosted release.** Recommended: not offered, with
   the refusals above and a follow-on stub, spec 118, for each tenant's
   own client.
   Alternative: build each tenant's own client now, in this spec, which
   needs specs 107 and 108 refined first and accepts the unconfirmed
   exemption. (A single operator-verified app is not offered as an
   alternative: the routed finding rules it out.)
2. **Telegram.** Recommended: each owner brings a bot. Alternative: one
   operator bot with chat linking, which adds an inbound surface.
3. **The save check.** Recommended: `getMe` and one confirmation message.
   Alternative: `getMe` and `getChat`, which sends nothing but rests on
   behavior not confirmed from the documentation.
4. **The digest on Postgres.** Recommended: say Gmail is not offered and
   omit `mail-watch` from the health lines. Alternative: leave the digest
   as is, which shows "None" for mail and a `mail-watch` line that never
   succeeds.
5. **The browser's half.** Recommended: a line in spec 114 for the
   settings form (Changes to approved specs). Alternative: its own stub, or the form in this spec, which
   adds `apps/web` to the diff.
   Taken 2026-10-11: the form is spec 114's, by its Amendment of
   2026-10-11 (D5).
6. **Operator variables on the hosted API.** Recommended: refuse to start.
   Alternative: ignore them and rely on spec 106's allowlist alone.

## Proof / origin

- `docs/adr/ADR-013-hosted-multi-tenant-deployment.md`, decisions 4, 7 and
  8, and Honest limitations (Gmail scopes).
- `specs/102-hosted-multi-tenant-deployment.md`: the stub's headline and the
  amended Local-first list.
- `specs/104-the-hosted-api-authenticates-every-request.md`: hosted mode,
  start-up refusals, run refusal until spec 106, `ErrorOut` and the
  contract rule for routes served in one mode.
- `specs/106-tenant-credentials-and-run-isolation.md`: the run environment
  allowlist, decryption at run start.
- `specs/116-tenant-credentials-are-stored-encrypted.md`: per-owner
  encrypted credentials and the `telegram` kind.
- `specs/112-the-tracker-domain-runs-on-either-store.md`: the CLI's Postgres
  refusal classes and `Connection.dialect`.
- `specs/018-gmail-watch.md`, `specs/040-mail-watch-and-durable-state.md`,
  `specs/049-inbox-page.md`, `specs/011-orchestrator-and-notify.md`,
  `specs/019-daily-digest.md`, `specs/029-run-outcome-visibility.md`: the watch, its state, the
  inbox, the notification and the digest as they stand. Their origin in the
  old system is `send_telegram.py` and `gmail_watch.py` under
  `~/job-hunt-local`, as those specs cite.
- Code: `services/api/src/harrier/notify.py`,
  `services/api/src/harrier/mail/watch.py`,
  `services/api/src/harrier/mail/run.py`,
  `services/api/src/harrier/digest.py`,
  `services/api/src/harrier_api/mail_routes.py`,
  `services/api/src/harrier_api/runs.py`,
  `services/api/src/harrier_cli/main.py`,
  `services/api/src/harrier/sources/__init__.py`, `config/schedule.json`,
  `.env.example`. Line numbers are from `main` at `44ac0d7`, except the
  `harrier_cli/main.py` ones, refreshed to `main` at `a7b9998`.

Vendor facts, each read 2026-10-10:

- Google, Gmail API scopes: `gmail.readonly` and `gmail.metadata`
  restricted, `gmail.labels` non-sensitive; restricted scopes need
  verification; storing or transmitting restricted data on servers needs a
  security assessment.
  https://developers.google.com/workspace/gmail/api/auth/scopes
- Google, restricted scope verification: annual assessment by an
  independent assessor approved by Google when data is accessed from or
  through a third-party server; exceptions for personal use ("used by only
  a few users, all of whom are known personally to you"), development and
  testing, service-owned data, internal use.
  https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification
- Google Cloud help, when verification is not needed: personal use "fewer
  than 100 users"; apps in development or testing still see the unverified
  app screen and the user cap.
  https://support.google.com/cloud/answer/13464323
- Google Cloud help, unverified apps: the unverified app screen and a cap
  of 100 new users for an unverified app.
  https://support.google.com/cloud/answer/7454865
- Google Cloud help, security assessment: annual revalidation, a Letter of
  Validation from an assessor, CASA framework; cost not stated.
  https://support.google.com/cloud/answer/13465431
- Google, Using OAuth 2.0: a refresh token expires in 7 days for an
  external app in Testing status; Gmail-scoped refresh tokens stop on a
  password change; six months unused; 100 refresh tokens per account per
  client. https://developers.google.com/identity/protocols/oauth2
- Telegram Bot API: requests to `https://api.telegram.org/bot<token>/METHOD`;
  `ok` and `description` in every response, `error_code` subject to
  change; `getMe` tests the token; `sendMessage` text 1 to 4096
  characters. https://core.telegram.org/bots/api
- Telegram Bots FAQ: about one message per second per chat, about 30 per
  second for bulk sends; bots are created with @BotFather.
  https://core.telegram.org/bots/faq

Assumptions, not confirmed from the pages read, for spec 110's staging
check:

- A bot cannot send to a private chat whose user has not written to it
  first, and `sendMessage` then answers `ok: false`. The `chat_unreachable`
  detail rests on it.
- A Fly machine reaches `api.telegram.org` with no extra egress setting.
- The token pattern `^\d+:[A-Za-z0-9_-]+$` matches every token @BotFather
  issues today; the Bot API page shows one example of that form.

Assumptions for the Gmail follow-on, checked there and not here: that an
unverified tenant client published to production keeps refresh tokens past
7 days, and how the personal use exemption applies to it.

## Out of scope

- Any change to what is sent. Nothing auto-sends stays as written, and so
  does "Telegram notifications are the only outbound messages".
- A hosted Gmail connection, per-owner mail state and a hosted mail
  schedule: spec 118.
- The browser's settings form: spec 114, its Amendment of 2026-10-11.
- How credentials are encrypted and where the key lives: spec 116.
- The hosted schedule: spec 108.
- Telegram inbound updates, commands or replies.
- Changing the local Telegram or Gmail setup, `.env.example`, or `harrier
  gmail-oauth`.
- Spec 040's unchecked timeout criteria for the mail client.

## Amendment (2026-10-13): delivered in parts, and what the first part found

Written in the change that builds the first part. The text above stands
except where this section says otherwise.

### Why in parts

Three things this spec relies on are not on `main` at `67032b8`:

- **Spec 116's credential store** (`put_credential`, `stored_credentials`,
  the `telegram` kind). It is draft PR #310 on
  `feat-116-credential-store`. Scope items 1 and 2, the three Telegram
  routes and `check_telegram`, write through it.
- **Spec 106's run environment.** `build_run_env` and the allowlist do not
  exist: hosted, every run is still refused (`RunsNotHosted`,
  `services/api/src/harrier_api/runs.py`). The criterion "A hosted run of
  owner A has A's `TELEGRAM_BOT_TOKEN`..." tests that module.
- **Spec 127's CLI classes.** On a Postgres URL the CLI runs `harrier
  store` and refuses every other command with `POSTGRES_NOT_YET`. Spec
  127 owns the class table, the walk and the local-only text, and already
  carries `gmail-watch` and `gmail-oauth` in its local-only class (its
  Scope item 7 and criterion). Its Amendment (2026-10-13) says a command
  outside the store and run classes keeps `POSTGRES_NOT_YET` until it
  lands, so this spec cannot give the two commands the local-only text
  first.

So this spec is delivered as follows. This splits the delivery, not the
spec; Akin may prefer to split the spec instead.

| Part | What | When |
|---|---|---|
| 1 (this change) | Scope items 3, 4 (API and run manager), 5, 6; the 501 on both mail routes in the contract; ADR-013 and `docs/architecture.md` for Gmail and the start refusal | now, on `main` |
| 2 | Scope items 1 and 2: the Telegram routes, `check_telegram`, their contract and tests, and the architecture line that they exist; the line numbers this spec cites (decision D9) | after PR #310 merges |
| spec 106 | the criterion on a hosted run's Telegram variables, moved there by its Amendment (2026-10-13, spec 109) | with spec 106 |
| spec 127 | `gmail-watch` and `gmail-oauth` refused with the local-only text, and the walk | with spec 127, as it already states |

Until part 2 lands, an owner has no way to save Telegram settings, so a
hosted run would notify nobody. No hosted run starts before spec 106
anyway.

### What part 1 found

1. **The start refusal's text.** Spec 104's start refusals are raised as
   `HostedConfigError` with no `error: ` prefix; the process ends on that
   exception's line. This one is built the same way: `the hosted API must
   not hold <NAME>; notification settings are per owner (spec 109)`
   (`services/api/src/harrier_api/hostedauth.py`). "Holds" means the name
   is present, set or empty: the name alone is the operator's, and an
   empty value today would be a filled one tomorrow
   (`services/api/tests/test_tenant_notifications.py::test_hosted_start_refuses_operator_notification_settings`,
   one case each and one empty `GMAIL_` case).
2. **`GmailNotHosted` is a `RunsNotHosted`.** So the app answers it with
   501 and its own detail through the handler that already answers every
   hosted run refusal, and `harrier_api/app.py` is not touched, as the
   diff criterion requires. The run manager checks it before the refusal
   spec 106 lifts (`refuse_every_hosted_run`), and the test runs with that
   refusal lifted
   (`services/api/tests/test_tenant_notifications.py::test_a_hosted_gmail_watch_run_is_refused_before_it_spawns`).
3. **The mail routes refuse as their first dependency.** Both routes raise
   `GmailNotHosted` hosted, before they read the archive or reach the run
   manager
   (`services/api/tests/test_tenant_notifications.py::test_hosted_mail_routes_answer_501`).
   `POST /mail/watch` therefore declares the Gmail 501 only, not the run
   refusal's. Spec 132's tests learned this:
   `services/api/tests/test_run_owner.py::test_starting_a_run_hosted_is_refused_until_spec_106`
   expects the Gmail detail from `POST /mail/watch` and no call to
   `start`, and
   `services/api/tests/test_run_owner.py::test_the_contract_declares_the_hosted_run_refusal`
   reads each 501 by its detail.
4. **A body that is not JSON.** Hosted, `POST /mail/watch` with a body
   that does not parse answers FastAPI's 422, because FastAPI reads the
   body before any dependency runs. It reads no file and starts no run.
   A parsed body, valid or not, gets the 501.
5. **The digest's line has no bullet.** "Updates needing action" holds
   exactly `Gmail watch is not offered on the hosted service.`, the text
   above, where the `None` line is `• None`
   (`services/api/tests/test_tenant_notifications.py::test_a_postgres_digest_reads_no_mail_file_and_says_why`).
   Spec 029's test of the health lines runs on both stores since spec
   126, and now expects `mail-watch` on SQLite only
   (`services/api/tests/test_run_outcome.py::test_the_digest_leads_with_every_scheduled_job`).
6. **The scrub test reads the record as notify emits it.** The root's
   handlers carry spec 133's scrub, which rewrites a record in place, so a
   test reading `caplog` passed with the scrub in `notify.py` removed. The
   test captures on the `harrier.notify` logger itself
   (`services/api/tests/test_tenant_notifications.py::test_a_telegram_failure_log_never_holds_the_token`).
7. **The test environment.** `child_environment` in
   `services/api/tests/test_hosted_auth.py` drops `TELEGRAM_` and `GMAIL_`
   names from the developer's shell, as it drops hosted variables, so a
   developer with either exported can still run the start tests.
8. **Spec 106's 501 text for `gmail-watch`.** Spec 106's kinds table words
   it `<kind> runs are not offered on the hosted service (spec 109)`. The
   run manager raises this spec's text, `Gmail watch is not offered on the
   hosted service (spec 109)`, before spec 106's table is read. Recorded
   in spec 106's amendment.

Under `services/api/src` part 1 touches `harrier/notify.py`,
`harrier/digest.py`, `harrier_api/runs.py`, `harrier_api/mail_routes.py`
and `harrier_api/hostedauth.py` (the module that holds spec 104's start
checks), all in the diff criterion's list.
