---
spec: 118
title: A tenant connects Gmail with their own Google client
status: accepted
approved: yes
milestone: M10
depends: [040, 106, 107, 108, 109, 116]
---

# Spec 118: A tenant connects Gmail with their own Google client

Split from spec 109 on 2026-10-10, when spec 109 was refined (its Scope
item 9 and Open decision 1). Spec 109 keeps Telegram per owner and refuses
Gmail on every hosted path.

## Problem

Gmail watch asks for one scope, `gmail.readonly`
(`services/api/src/harrier/mail/watch.py:36`), which Google classes as
restricted. An app that offers a restricted scope to other people needs
restricted scope verification, and an annual security assessment when it
stores or transmits the data on servers (Google's Gmail API scopes page).
A hosted watch would fetch messages on Fly and keep its state in
Supabase, so spec 109 answered that it does both, and kept Gmail out of
the first hosted release.

The remaining path is each tenant bringing their own Google OAuth client.
Spec 109 found it workable but unconfirmed:

- An app in Testing status with an external user type gets refresh tokens
  that expire in 7 days, so a watch on such a client stops weekly (Google,
  Using OAuth 2.0).
- Published unverified, the client relies on the personal use exemption,
  which Google words two ways ("fewer than 100 users"; "used by only a few
  users, all of whom are known personally to you"). No page read for spec
  109 says how it applies when the data passes through servers the tenant
  does not run.
- The watch today is one person's: one token file refreshed in place
  (`watch.py:595-602`, `:643-645`), a loopback grant flow on the
  operator's machine (`services/api/src/harrier_cli/main.py:889-928`), and
  state files in the data directory (`watch.py:163-172`). Line numbers are
  from `main` at `44ac0d7`.

## Scope

Stub created by spec 109's refinement to sequence the hosted build. It is
refined into a real spec, with behavior and failure modes, before approval
is asked for.

## Acceptance criteria

Headline, to be expanded into checkable criteria when refined: each owner
connects Gmail with an OAuth client in their own Google Cloud project,
granting `gmail.readonly` to their own app, never to a shared harrier
client. The client id, client secret and refresh token are spec 116
credentials, given to the watch's run by spec 106. The grant uses a web
flow with a redirect to the hosted API, state and PKCE, not `harrier
gmail-oauth`'s loopback flow. A refreshed access token stays in the run's
memory and is never written back. An expired or revoked refresh token
(Testing status after 7 days, a password change, six months unused,
revocation, the 100-token limit per client) is reported to the owner as
"reconnect Gmail", never retried in a loop. The seen state and the events
archive move to the owner's prefix through spec 107's seam, keeping spec
040's rules: an absent object is a first run, a damaged one is refused,
never read as empty, and the archive stays redacted and bounded. Spec 108
runs at most one watch per owner at a time, so two runs cannot overwrite
each other's seen state. Before approval this spec records, on spec 110's
staging project, whether a tenant client published unverified keeps its
refresh token past 7 days, and states what is known of the personal use
exemption for a client whose data passes through the operator's servers.

## Proof / origin

- `specs/109-gmail-and-telegram-for-tenants.md`: Problem ("What Google
  requires"), Behavior ("Gmail in the first hosted release" and "The Gmail
  follow-on"), Options weighed (Gmail, option b) and Open decision 1.
- `specs/018-gmail-watch.md` and `specs/040-mail-watch-and-durable-state.md`:
  the watch and its durable state.
- `docs/adr/ADR-013-hosted-multi-tenant-deployment.md`, Honest
  limitations (Gmail scopes).
- Google, Gmail API scopes, read 2026-10-10:
  https://developers.google.com/workspace/gmail/api/auth/scopes
- Google, restricted scope verification, read 2026-10-10:
  https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification
- Google, Using OAuth 2.0, read 2026-10-10:
  https://developers.google.com/identity/protocols/oauth2

## Out of scope

- One harrier Google app verified by the operator: ruled out by the
  post-merge review spec 109 cites.
- Telegram per owner and the refusals of hosted Gmail: spec 109.
- Any change to the local watch, `harrier gmail-oauth` or `.env`.
