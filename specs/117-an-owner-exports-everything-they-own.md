---
spec: 117
title: An owner exports everything they own
status: proposed
approved: no
milestone: M10
depends: [104, 105, 107, 111, 112, 116]
---

# Spec 117: An owner exports everything they own

Split from spec 111 on 2026-10-10, when spec 111 was refined (its Open
decision 1). Spec 111 keeps sign-up and deletion.

## Problem

A hosted service holds other people's personal data, and they need a way
to take it out. ADR-013's Consequences say a tenant's data can be exported
and deleted on request. Akin chose invite-only sign-up until export and
deletion ship (spec 102, open decision 5).

The target users are in the EU (spec 102, open decision 2). GDPR Article
20(1) asks for personal data in "a structured, commonly used and
machine-readable format". Nothing in harrier gives a hosted owner their
data in any format: `harrier export` writes CSV and profile files to a
directory on the local machine and stays local (spec 112, Out of scope).

Export and deletion were one stub (spec 111). They are two reviews.
Deletion is a database change and operator commands holding a secret key.
Export reads only what the owner may already read, through the API as the
owner, and changes the contract. It also waits on Storage (spec 107) and
on the browser (spec 114).

## Scope

Stub created by spec 111's refinement to sequence the hosted build. It is
refined into a real spec, with behavior and failure modes, before approval
is asked for.

## Acceptance criteria

Headline, to be expanded into checkable criteria when refined: an owner
downloads everything they own from the hosted API, running as
`harrier_tenant` under the row policy, with no service-role privilege. The
file is a zip holding a `manifest.json` (format version, export time,
store version, the list of tables and objects), one JSON Lines file per
table in `OWNED_TABLES` with every column but `owner_id`, and every
Storage object under the owner's prefix at its own path. The format is
documented in `docs/` and versioned. Stored credentials (spec 116) are
listed by kind, never exported by value. The SPA's control for it is spec
114's or later. `harrier export` stays local (spec 112).

## Proof / origin

- `specs/111-sign-up-export-and-deletion.md`: Problem ("Export and
  deletion are two reviews"), Behavior ("Export (moved to spec 117)") and
  Open decision 1.
- `docs/adr/ADR-013-hosted-multi-tenant-deployment.md`: Consequences.
- `specs/102-hosted-multi-tenant-deployment.md`: open decisions 2 and 5.
- `specs/105-every-personal-table-is-owned-and-policed.md`: the owned
  tables and their policy.
- `specs/107-artifacts-and-run-state-in-storage.md`: the owner's prefix in
  `harrier-files` and the seam that reads it.
- `specs/112-the-tracker-domain-runs-on-either-store.md`, Out of scope: a
  tenant export, and `harrier export` staying local.
- GDPR Article 20(1), read 2026-10-10: https://gdpr-info.eu/art-20-gdpr/

## Out of scope

- Deletion, sign-up and invitations: spec 111.
- Importing an export into another store or into the local product.
- The local `harrier export`, which does not change.
- Exporting stored credential values.
