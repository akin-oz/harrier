---
spec: 108
title: Hosted scheduling
status: accepted
approved: yes
milestone: M10
depends: [106, 107]
---

# Spec 108: Hosted scheduling

## Problem

launchd runs the schedule on one Mac (ADR-006). The hosted deployment has no
launchd and many owners. ADR-013 decision 4 has the scheduler list due
owners with the service role and start each run as its owner.

## Scope

Stub created by spec 102 to sequence the hosted build. It is refined into a
real spec, with behavior and failure modes, before approval is asked for.

## Acceptance criteria

Headline, to be expanded into checkable criteria when refined: Each due
owner's run starts as that owner; a failed run for one owner is reported and
does not block the others; domain code never runs as the service role.

## Proof / origin

ADR-013 decision 4; ADR-006.

## Out of scope

Changing the local schedule.
