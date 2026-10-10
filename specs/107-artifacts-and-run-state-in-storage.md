---
spec: 107
title: Artifacts and run state in Storage
status: proposed
approved: no
milestone: M10
depends: [105, 106]
---

# Spec 107: Artifacts and run state in Storage

## Problem

Resumes, letters, answers, drafts, summaries, seen-state and scoring models
are files on the local disk. A hosted API machine has no per-owner disk.
ADR-013 decision 6 moves them to a private Supabase Storage bucket under
owner-prefixed paths.

## Scope

Stub created by spec 102 to sequence the hosted build. It is refined into a
real spec, with behavior and failure modes, before approval is asked for.

## Acceptance criteria

Headline, to be expanded into checkable criteria when refined: File writes
go through one storage seam: the local file system locally, the owner-
prefixed private bucket hosted, with a storage policy matching spec 105.
A track id is no longer unique across owners on Postgres (spec 105), so
every path built from one, such as academic discovery's run and incoming
directories (`services/api/src/harrier/academic/discovery.py:153`, `:172`),
sits under the owner's prefix hosted.

## Proof / origin

ADR-013 decision 6.

## Out of scope

Backups of the bucket (spec 110).
