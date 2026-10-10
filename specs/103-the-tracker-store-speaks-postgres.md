---
spec: 103
title: The tracker store speaks Postgres
status: proposed
approved: no
milestone: M10
depends: [090, 091, 102]
---

# Spec 103: The tracker store speaks Postgres

## Problem

The tracker's schema and migrations are SQLite only (ADR-003, spec 090).
ADR-013 decision 5 needs one schema definition that produces SQLite and
Postgres DDL, a migration runner that applies either, and one write path for
both. Akin chose SQLite locally and Postgres hosted (spec 102, open decision
1).

## Scope

Stub created by spec 102 to sequence the hosted build. It is refined into a
real spec, with behavior and failure modes, before approval is asked for.

## Acceptance criteria

Headline, to be expanded into checkable criteria when refined: One schema
definition produces SQLite and Postgres DDL; the migration runner applies
either whole or not at all; the tracker tests pass against both.

## Proof / origin

ADR-013 decisions 5 and 9; spec 090; spec 102 open decision 1.

## Out of scope

Authentication, owner columns and row-level policy (specs 104 and 105).
Hosting.
