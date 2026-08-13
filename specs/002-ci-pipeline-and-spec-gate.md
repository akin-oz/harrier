---
spec: 002
title: CI pipeline and spec-trailer gate
status: shipped
approved: yes
milestone: M0
depends: [001]
---

# Spec 002: CI pipeline and spec-trailer gate

## Problem

No CI exists. The governance chain needs its authoritative half: gates that make wrong un-mergeable.

## Scope

- GitHub Actions: PR workflow running just check recipes (type-check x2, lint x2, tests x2)
- contract-drift job (regenerate and git diff, ADR-005)
- aie check via akin-oz/ai-engineering@v0
- gitleaks job on PR and main
- spec-gate job resolving every commit's Spec: NNN trailer to an approved spec (Sorrel spec-gate.yml mechanics)

## Acceptance criteria

- [ ] a PR with a commit missing a trailer fails the spec-gate job
- [ ] a PR referencing an unapproved spec fails
- [ ] all jobs green on the scaffold

## Proof / origin

the Sorrel spec-gate workflow (a private sibling project of the author's)

## Out of scope

To be refined before approval. This stub sequences the backlog; scope narrows or
splits when the spec is drafted for real.
