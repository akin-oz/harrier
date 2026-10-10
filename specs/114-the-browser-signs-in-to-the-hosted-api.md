---
spec: 114
title: The browser signs in to the hosted API
status: proposed
approved: no
milestone: M10
depends: [104]
---

# Spec 114: The browser signs in to the hosted API

## Problem

Spec 104 makes the hosted API refuse any request without a verified
Supabase token. The SPA in `apps/web` has no way to get one: it sends the
local API token of spec 035, assumes the API shares its origin, and reads
run events with `EventSource`, which cannot send an `Authorization`
header. Spec 104's Open decision 8 put the browser's half here.

## Scope

Stub created by spec 104 to take the browser's half of hosted
authentication. It is refined into a real spec, with behavior and failure
modes, before approval is asked for.

## Acceptance criteria

Headline, to be expanded into checkable criteria when refined: in hosted
mode the SPA signs in through Supabase Auth, sends the session's access
token as a bearer token on every API call through the generated contract
client, reads the API base URL from build configuration, streams run
events with `fetch` in both modes, and offers a capture page the
bookmarklet can open; the local mode keeps working exactly as today.

## Proof / origin

`specs/104-the-hosted-api-authenticates-every-request.md`, Scope and Open
decisions 4, 6 and 8; ADR-005 (the SPA speaks only generated types);
ADR-013 decision 7 (what Vercel holds).

## Out of scope

Server-side verification (spec 104). Sign-up and invitations (spec 111).
