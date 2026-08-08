---
spec: 003
title: Classification coverage and secret scanning
status: shipped
approved: yes
milestone: M0
depends: [001]
---

# Spec 003: Classification coverage and secret scanning

Amended 2026-08-08 per ADR-008: the original encrypted-in-repo scope (SOPS with
age, `private/`, `just decrypt`) shipped and was then removed the same day when
Akin decided personal data must not enter git in any form. Personal data lives
in the local database instead (profile tables land with spec 004). This spec now
covers classification and scanning only.

## Problem

The repo is public. Personal data and credentials must be provably absent from
git, and that absence must be enforced by tests and scanners, not discipline.

## Scope

- config/data-classification.json: two classes (public, never-in-git)
- tests/test_classification_coverage.py: never-in-git patterns gitignored and absent
  from the index; encrypted-layer artifacts must not return
- .gitignore and .env.example
- gitleaks config with project-specific token patterns
- just backup: timestamped local archive of all personal data (ADR-008)

## Acceptance criteria

- [x] coverage test green and running in CI
- [x] a tracked file matching a never-in-git pattern fails the coverage test
- [x] gitleaks catches a planted realistic Apify token in pre-commit and CI

## Proof / origin

docs/adr/ADR-002-data-at-rest.md; docs/adr/ADR-008-personal-data-in-database.md

## Out of scope

Profile tables and the import from the old repo (spec 004). Local disk
encryption and backup custody (user responsibility, docs/privacy-plan.md).
