---
spec: 112
title: The tracker domain runs on either store
status: proposed
approved: no
milestone: M10
depends: [103]
---

# Spec 112: The tracker domain runs on either store

## Problem

Spec 103 creates a Postgres store with the tracker's schema, and nothing
uses it. The domain's SQL is written for `sqlite3`: `?` placeholders,
`datetime('now')`, `COLLATE NOCASE`, `cursor.lastrowid`,
`sqlite3.IntegrityError`, `BEGIN IMMEDIATE` as the write lock, and
`sqlite3.Row` read both by position and by name. It lives in nine modules
(`tracker/store.py`, `tracks.py`, `userconfig/store.py`, `profile/store.py`,
`runoutcome.py`, `mail/watch.py`, `logredact.py`, `apply/profile.py`,
`resume/content.py`), and about thirty more annotate `sqlite3.Connection`.
The SQLite write lock is database wide; on a store many owners share it
would serialize every owner's writes.

## Scope

Stub created by spec 103 to take the domain half of the Postgres port. It
is refined into a real spec, with behavior and failure modes, before
approval is asked for.

## Acceptance criteria

Headline, to be expanded into checkable criteria when refined: every
tracker read and write runs on either store through one connection seam;
the tracker store, tracks, user configuration, profile and job event tests
pass against both dialects; a concurrent decision on one job waits for the
first on both; refusals reach callers as the same exception on both; and
backup, doctor's integrity check, cutover, legacy import and the host lease
refuse on Postgres with a message saying they are local only.

## Proof / origin

`specs/103-the-tracker-store-speaks-postgres.md`, Problem and Out of scope;
ADR-013 decision 5; spec 079's amendment for the write lock.

## Out of scope

Authentication and roles (spec 104), `owner_id` and row-level policy
(spec 105).
