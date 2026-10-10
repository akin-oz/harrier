---
spec: 106
title: Tenant credentials and run isolation
status: accepted
approved: yes
milestone: M10
depends: [104, 105]
---

# Spec 106: Tenant credentials and run isolation

## Problem

Credentials live in one `.env`, and a run's subprocess inherits the server's
whole environment (spec 035). Hosted, that would hand one tenant's run every
tenant's keys. ADR-013 decision 8 stores credentials per owner and gives
each run only its owner's. Akin chose bring-your-own LLM keys (spec 102,
open decision 3).

## Scope

Stub created by spec 102 to sequence the hosted build. It is refined into a
real spec, with behavior and failure modes, before approval is asked for.

## Acceptance criteria

Headline, to be expanded into checkable criteria when refined: Credentials
are stored per owner, encrypted at rest; a run records its owner and its
environment holds only that owner's credentials; hosted offers only API LLM
providers. A track id is no longer unique across owners on Postgres (spec
105), so every run key built from one, such as academic discovery's
`discovery:<track id>` (`services/api/src/harrier/academic/discovery.py:71`),
is qualified by owner hosted.

## Proof / origin

ADR-013 decisions 4 and 8; spec 102 open decision 3; `.env.example`.

## Out of scope

Gmail and Telegram setup flows (spec 109). Operator-paid LLM calls.
