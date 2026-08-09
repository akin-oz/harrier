---
spec: 023
title: User configuration in the database
status: proposed
approved: no   # only Akin flips this to yes
milestone: M5
depends: [004, 011]
---

# Spec 023: User configuration in the database

Stub created from ADR-009; refine before approval.

## Problem

User configuration (the board watchlist, LinkedIn searches, discovery
settings, the hold list) lives in gitignored loose files. ADR-009 wants it
in the database: customization through the GUI, a clean open-source story,
and a data layer that a tenant scope can partition later.

## Scope (to refine)

- A configuration store in the database (likely alongside
  profile_documents, keyed by kind plus a future-proof scope column).
- Feeds, LinkedIn search URLs, discovery settings, and the hold list read
  through accessors backed by the store; the file loaders become one-shot
  import commands (harrier config import) and stay as the demo fallback.
- API endpoints and GUI editing (the old Streamlit scoring-settings page's
  successor grows configuration tabs).
- Migration for cutover: import the real files once (spec 022 gains this
  step).

## Acceptance criteria (to refine)

- [ ] Discovery reads feeds and searches from the store; a fresh clone with
      no files and no store rows runs cleanly with empty sources
- [ ] Config editable through the GUI and the CLI
- [ ] Import command round-trips the current files
- [ ] All gates green on PR

## Proof / origin

docs/adr/ADR-009-user-configuration-and-tenancy.md; ADR-008; the spec 011
review thread that prompted the reclassification.

## Out of scope

Authentication, tenant isolation, hosting (a future multi-tenant ADR);
per-tenant scoping beyond the schema being partitionable.
