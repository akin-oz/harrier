---
spec: 021
title: Demo mode, fixtures, public README
status: accepted
approved: yes
milestone: M5
depends: [005,006]
---

# Spec 021: Demo mode, fixtures, public README

## Problem

A stranger clones, runs one command, and sees the system work on synthetic data.

## Scope

- fixtures: fake candidate profile, synthetic jobs, recorded-then-scrubbed importer responses
- just demo: seeded temp store, API serving the built SPA, external services stubbed
- README at Sorrel caliber: sixty-second what-it-is, architecture, the governance chain as a feature, honest limitations
- privacy-reviewer pass over every fixture

## Acceptance criteria

- [ ] clean-machine clone-to-demo works with zero keys and no decryption
- [ ] pre-publish checklist (docs/privacy-plan.md) executed and green

## Proof / origin

docs/privacy-plan.md; /Users/akinoztorun/Documents/projects/sorrel/README.md

## Out of scope

To be refined before approval. This stub sequences the backlog; scope narrows or
splits when the spec is drafted for real.
