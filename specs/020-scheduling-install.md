---
spec: 020
title: Schedule install CLI and launchd templates
status: accepted
approved: yes
milestone: M5
depends: [011,019]
---

# Spec 020: Schedule install CLI and launchd templates

## Problem

Generated plists kill the wrong-path bug class; policy moves into the CLI (ADR-006).

## Scope

- harrier schedule install/status/uninstall rendering plists from templates (real path and user)
- cadence config in one file: discovery 09:00 13:00 16:00 20:00, digest 20:30, gmail watch 300s
- GUI schedule status and launchctl controls

## Acceptance criteria

- [ ] install writes and loads three plists pointing at the harrier repo
- [ ] status shows next-run times in the GUI

## Proof / origin

launchd/*.plist (old repo, incl. the stale-path defect); docs/adr/ADR-006-scheduling.md

## Out of scope

To be refined before approval. This stub sequences the backlog; scope narrows or
splits when the spec is drafted for real.
