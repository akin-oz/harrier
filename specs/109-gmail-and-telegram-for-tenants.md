---
spec: 109
title: Gmail and Telegram for tenants
status: accepted
approved: yes
milestone: M10
depends: [106]
---

# Spec 109: Gmail and Telegram for tenants

## Problem

Gmail watch and Telegram notifications are configured for one person through
local files and `.env`. Hosted tenants need their own. Google's restricted
Gmail scopes need app verification before external users can grant them
(ADR-013, Honest limitations).

## Scope

Stub created by spec 102 to sequence the hosted build. It is refined into a
real spec, with behavior and failure modes, before approval is asked for.

## Acceptance criteria

Headline, to be expanded into checkable criteria when refined: Each owner
connects their own Telegram bot and Gmail account, or the spec records that
Gmail is not offered in the first hosted release and why.

## Proof / origin

ADR-013 decision 8 and Honest limitations.

## Out of scope

Any change to what is sent. Nothing auto-sends stays as written.
