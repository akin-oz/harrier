---
spec: 105
title: Every personal table is owned and policed
status: proposed
approved: no
milestone: M10
depends: [103, 104]
---

# Spec 105: Every personal table is owned and policed

## Problem

In a shared Postgres store, one owner's rows sit beside another's. ADR-013
decision 1 gives every personal table an `owner_id` and a forced row-level
policy, so the database refuses cross-owner reads even when code forgets.

## Scope

Stub created by spec 102 to sequence the hosted build. It is refined into a
real spec, with behavior and failure modes, before approval is asked for.

## Acceptance criteria

Headline, to be expanded into checkable criteria when refined: Every table
with personal data has `owner_id` and a forced policy; a test fails if one
does not; owner A reads zero rows of owner B.

## Proof / origin

ADR-013 decisions 1 and 2; ADR-012 point 2.

## Out of scope

Storage object policies (spec 107). Credentials (spec 106).
