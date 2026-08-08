---
spec: 003
title: Encryption setup and classification coverage
status: accepted
approved: yes
milestone: M0
depends: [001]
---

# Spec 003: Encryption setup and classification coverage

## Problem

PII must be encrypted-in-repo and credentials kept out of git before any real data enters the repo (ADR-002).

## Scope

- age keypair generation procedure (documented, key never committed)
- .sops.yaml creation rules for private/
- config/data-classification.json (machine-readable classification table from ADR-002)
- tests/test_encryption_coverage.py: every encrypted-in-repo path matches a sops rule and is ciphertext in HEAD; every never-in-git path is gitignored and absent from the index
- .gitignore and .env.example
- gitleaks config with project-specific token patterns
- just decrypt recipe writing private/decrypted/ (gitignored)

## Acceptance criteria

- [ ] coverage test green and running in CI
- [ ] a plaintext file added under private/ fails the coverage test
- [ ] gitleaks catches a planted fake Apify token in pre-commit and CI

## Proof / origin

docs/adr/ADR-002-data-at-rest.md

## Out of scope

To be refined before approval. This stub sequences the backlog; scope narrows or
splits when the spec is drafted for real.
