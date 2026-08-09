# ADR-009: User configuration is data, and the path to multi-tenancy

- Status: accepted
- Date: 2026-08-09
- Extends: ADR-008 (personal data in the local database)

## Context

Akin set two goals during the M2 review: the repo should be cleanly open
sourced, and the system should become easily customizable and eventually
multi-tenant. Both break an assumption the classification inherited from
the old repo: that curated config files (the board watchlist, the LinkedIn
searches, discovery settings) are "public config". They are not: they are
one user's data. A stranger cloning the repo should get example templates
and an empty system, not Akin's watchlist.

## Decision

1. **User configuration is user data.** config/feeds.txt,
   config/linkedin_search_urls.txt, config/discovery.json, and
   config/companies-hold.csv are never-in-git (classification updated).
   Committed `.example` files document every shape. This lands immediately
   (spec 011's PR).
2. **Configuration moves into the database.** A spec (023, approved
   separately) migrates user configuration from loose files into the
   store, alongside the profile documents (ADR-008), with API endpoints
   and GUI editing. File loaders remain as import paths, not as the source
   of truth. Customizing the system then means editing rows through the
   GUI, not editing files in a checkout.
3. **Tenant-ready, not tenant-complete.** Multi-tenancy is a direction,
   not this milestone: authentication, per-tenant isolation, and hosting
   are out of scope for the parity rewrite (M0 to M5 target cutover of a
   single-user tool). What changes now is that nothing new may *block*
   tenancy: configuration and personal data live in the database keyed so
   a tenant scope can partition them later, code reads configuration
   through accessors rather than module-level file paths, and "the
   candidate" stays a data record, never a constant.

## Consequences

- The pre-publish story improves: the repo carries zero user data of any
  kind, and open sourcing needs no judgment calls about watchlists.
- Spec 023 (user configuration in the database) enters the backlog with
  approved: no; Akin sequences it (natural slot: after M3, or alongside
  spec 021's demo mode, which benefits directly).
- A future multi-tenant ADR owns auth, isolation, and hosting when that
  direction becomes concrete; this ADR only guarantees the data layer will
  not fight it.
- The cutover plan (spec 022) adds migrating the real config files into
  the database once spec 023 lands.
