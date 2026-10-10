---
spec: 109
title: Gmail and Telegram for tenants
status: proposed
approved: no
milestone: M10
depends: [106]
---

# Spec 109: Gmail and Telegram for tenants

## Problem

Gmail watch and Telegram notifications are configured for one person through
local files and `.env`. Hosted tenants need their own. Google's restricted
Gmail scopes need app verification before external users can grant them
(ADR-013, Honest limitations).

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

## Scope

Stub created by spec 102 to sequence the hosted build. It is refined into a
real spec, with behavior and failure modes, before approval is asked for.

## Acceptance criteria

Headline, to be expanded into checkable criteria when refined: Each owner
connects their own Telegram bot and Gmail account, or the spec records that
Gmail is not offered in the first hosted release and why. If Gmail is
offered, each tenant connects it through their own Google OAuth client,
never one shared harrier client asking for `gmail.readonly` (post-merge
review of PRs #194 and #207, 2026-10-10).

## Proof / origin

ADR-013 decision 8 and Honest limitations; the post-merge review of PRs
#194 and #207 (2026-10-10); Google's Gmail API scopes page (above).

## Out of scope

Any change to what is sent. Nothing auto-sends stays as written.
