---
spec: 110
title: Deploy pipeline and environments
status: proposed
approved: no
milestone: M10
depends: [103, 104, 105]
---

# Spec 110: Deploy pipeline and environments

## Problem

No hosted environment exists. ADR-013 decision 7 names Fly, Vercel and
Supabase. Akin chose staging and production (spec 102, open decision 4) in
the EU (open decision 2).

## Scope

Stub created by spec 102 to sequence the hosted build. It is refined into a
real spec, with behavior and failure modes, before approval is asked for.

## Acceptance criteria

Headline, to be expanded into checkable criteria when refined: Staging and
production each have a Supabase project, a Fly app and a Vercel environment
in the EU; migrations apply before a new API version serves; no secret
enters git.

## Proof / origin

ADR-013 decision 7; spec 102 open decisions 2 and 4.

## Out of scope

Pricing and billing.
