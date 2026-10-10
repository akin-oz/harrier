---
spec: 104
title: The hosted API authenticates every request
status: proposed
approved: no
milestone: M10
depends: [035, 083, 084, 103]
---

# Spec 104: The hosted API authenticates every request

## Problem

The API has no user accounts and trusts the local host (spec 035). Hosted,
it is reachable from the internet. ADR-013 decision 3 requires every request
to carry a verified Supabase JWT and to run its transaction as the
`authenticated` role.

## Scope

Stub created by spec 102 to sequence the hosted build. It is refined into a
real spec, with behavior and failure modes, before approval is asked for.

## Acceptance criteria

Headline, to be expanded into checkable criteria when refined: A request
without a valid token answers 401 and opens no transaction; a valid one runs
as its user; the spec names which spec 035 guards carry over.

## Proof / origin

ADR-013 decisions 3 and 4; spec 035.

## Out of scope

Row-level policies (spec 105). Sign-up rules (spec 111).
