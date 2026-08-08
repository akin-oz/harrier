---
spec: 001
title: Repo scaffold and toolchain
status: accepted
approved: yes
milestone: M0
depends: []
---

# Spec 001: Repo scaffold and toolchain

## Problem

The monorepo skeleton from ADR-007 does not exist yet: no Python project, no web app shell, no task runner, no pre-commit wiring.

## Scope

- uv project in services/api with harrier, harrier_api, harrier_cli package skeletons (no domain logic)
- pnpm workspace with apps/web (Vite React strict TS shell) and packages/contract (empty)
- justfile: dev, demo (stub), check, gate, contract (stub) recipes
- lefthook: gitleaks, ruff, eslint/prettier on staged files, commit-msg Spec trailer shape check
- ruff, pyright strict, eslint flat config with FSD boundaries, prettier, vitest, pytest wired

## Acceptance criteria

- [ ] just check runs both type-checks, both linters, both (empty) test suites and exits 0
- [ ] just gate exists so the turn-end hook stops passing vacuously
- [ ] lefthook blocks a commit without a Spec: NNN trailer from a plain terminal

## Proof / origin

docs/adr/ADR-007-repo-layout-and-toolchain.md

## Out of scope

To be refined before approval. This stub sequences the backlog; scope narrows or
splits when the spec is drafted for real.
