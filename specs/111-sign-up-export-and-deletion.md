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

## Scope

Stub created by spec 102 to sequence the hosted build. It is refined into a
real spec, with behavior and failure modes, before approval is asked for.

## Acceptance criteria

Headline, to be expanded into checkable criteria when refined: Accounts are
created by invitation only; an owner can export everything they own and
delete it, rows and storage objects both.

## Proof / origin

ADR-013 Consequences; spec 102 open decision 5.

## Out of scope

Open sign-up. Billing.
