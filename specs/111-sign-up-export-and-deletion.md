---
spec: 111
title: Sign-up, export and deletion
status: proposed
approved: no
milestone: M10
depends: [104, 105, 107]
---

# Spec 111: Sign-up, export and deletion

## Problem

A hosted service holds other people's personal data. They need a controlled
way in and a way to take their data out or remove it. Akin chose invite-only
sign-up until export and deletion ship (spec 102, open decision 5).

The post-merge review of PRs #194 and #207 (architecture 5, 2026-10-10)
found that deletion collides with the store's own refusals. `job_events`
is append-only and `tracks` are never deleted, enforced on Postgres by
`harrier_refuse()` (`services/api/src/harrier/tracker/schema.py:441-445`)
behind row triggers (`:463`, `:545`, `:549`) and, since spec 105, statement
triggers on TRUNCATE (`:776`, `:780`). An owner's events and tracks cannot
be deleted at all. Disabling the triggers takes a lock on the shared
table, which every other owner's writes then wait for. The proposed
answer: `harrier_refuse()` stands aside only when a transaction-local
setting is on, and only a deletion function restricted to the service
role sets it.

## Scope

Stub created by spec 102 to sequence the hosted build. It is refined into a
real spec, with behavior and failure modes, before approval is asked for.

## Acceptance criteria

Headline, to be expanded into checkable criteria when refined: Accounts are
created by invitation only; an owner can export everything they own and
delete it, rows and storage objects both. Deleting an auth user who still
owns rows is refused by the `owner_id` foreign key, which has no `ON DELETE`
action (spec 105). How deletion orders the rows and the user is this
spec's to decide. Deleting an owner's events and tracks neither disables
a trigger nor locks the shared table: `harrier_refuse()` stands aside
only under a transaction-local setting that a service-role-only deletion
function sets, and every other session is still refused (post-merge
review of PRs #194 and #207, 2026-10-10).

## Proof / origin

ADR-013 Consequences; spec 102 open decision 5; the post-merge review of
PRs #194 and #207 (2026-10-10).

## Out of scope

Open sign-up. Billing.
