# ADR-012: Search tracks are rows; tenants are data directories

- Status: accepted; hosted half decided by ADR-013
- Date: 2026-10-08
- Extends: ADR-009 (isolation, which its point 3 handed to a future ADR)
- Preserves: ADR-003 (one write path, now per store), ADR-008, ADR-011
- Source: specs/091-every-tracker-row-belongs-to-a-search-track.md, which carries this text and the migration it decides

## Context

ADR-003 made SQLite the tracker's source of truth: one file, one write
path. ADR-008 put every personal document in that file. ADR-009 made user
configuration data in the same file and promised that nothing would block
tenancy, then (amended by spec 041) withdrew the one column that pretended
to deliver it. ADR-011 made one kernel the owner of the file at a time.

Two directions now need a decision that those four ADRs left open:

1. A second kind of search for the same person, with its own rules,
   sources and documents, inside the same install.
2. A second person. Locally, that is a second install on the same machine
   or a second data directory. Hosted, it is a service with accounts, which
   is the deployment the product is meant to reach.

The question is which boundary each of these crosses, because the answer
decides where a predicate lives. A boundary crossed by a row needs a column
and a `WHERE` clause in every reader, forever. A boundary crossed by a store
needs none of that: the reader cannot see what it was not opened on.

## Options weighed

**(a) Tracks plus a `tenant_id` column on every personal table.** One
file holds everyone. Every reader filters by two keys. A forgotten
predicate leaks one person's rows to another, and SQLite has no row-level
policy to catch it, so the discipline is in every query. The column holds
one value until a second person exists, which is the shape spec 041 removed.
Backup, restore, `doctor`, export and redaction all have to learn tenancy
on the day the column lands. Rejected.

**(b) One SQLite file per tenant, tracks inside it as rows.** Tenant
isolation is the file system: a process opened on one directory cannot read
another. The domain's SQL names a track and never a tenant. The current
install is already the default tenant, in place, with nothing to migrate.
Backup and restore of one person are a directory copy. Spec 041's bar is
met: `track_id` takes its second value in spec 093, and no tenant column
exists until a tenant does. The costs: tenant selection becomes a question
at every process boundary (a flag or environment value for the CLI, the
session for the API, the plist for launchd); a cross-tenant operation (none
exists) is N opens; and where the other directories live matters, because
`just backup` copies `data/` as a tree and would raw-copy a nested live WAL.
Chosen for the local product.

**(c) A shared schema with a users table and a scoped repository.** Every
store function takes a user, and the store applies it. This is (a) with the
discipline moved into one module, and it is the shape a hosted service on a
database with row-level policies ends up with. On SQLite it has (a)'s costs
without the database's help. Not chosen for the local product; it is the
shape ADR-013 starts from for the hosted one.

## Decision

1. **A search track is a row.** `tracks` is a table; `jobs.track_id` names
   one; every reader of tracker rows reads one track (spec 092). This holds
   in every deployment, local or hosted, because a track is something one
   person has several of.
2. **A tenant is a store boundary, never a predicate the domain writes.**
   Locally, the boundary is a data directory with its own `tracker.db`; the
   checkout's `data/` is the default tenant, in place. In a hosted
   deployment the boundary is the database's own row-level policy, keyed by
   the authenticated owner, applied by the database from the session and
   not by the domain's SQL. ADR-013 decides that host, its authentication,
   and its schema; it inherits from this ADR that domain code filters by
   track and never by tenant.
3. **Nothing in the local schema names a tenant.** No `tenant_id`, no
   `owner`, no users table, until ADR-013 adds them to the hosted schema,
   which spec 041 already records as a migration and an accepted cost.
4. **"One tracker, one write path" holds per store.** Each tenant's store
   has exactly one tracker and is written by exactly one module,
   `harrier.tracker`. The invariant's words do not change; ADR-003 gains a
   note saying which file "the tracker" is: the one in the data directory
   the process was opened on.
5. **Schema constructs are chosen with a port in mind.** The DDL here is
   SQLite's. A table, a CHECK, a trigger, an index and a partial unique
   index each have a direct equivalent in the database ADR-013 is likely
   to choose; the one SQLite-only construct, `GLOB` in the slug CHECK, has
   a regular-expression CHECK as its equivalent, and the slug rule is also
   enforced in `harrier.tracks` so the CHECK is a backstop and not the only
   copy of the rule.

## Consequences

- Migration 8 (below) adds `tracks` and `jobs.track_id`, and seeds the one
  track every existing row belongs to.
- Spec 092 makes every reader take a scope. Spec 093 creates the second
  track and gives the CLI a way to name one. Both are the proof that the
  column is not speculative.
- Where other tenant directories live when a second local tenant exists
  is deferred to that day, with two constraints already fixed. Any location
  inside the checkout is classified never-in-git in
  `config/data-classification.json` and matched by `.gitignore` before the
  directory can exist, because a second person's `tracker.db` beside
  `data/` would otherwise be staged by `git add -A` (today only `/data/`
  and its descendants are ignored). And the backup and restore commands
  learn to skip or include those directories before any can exist,
  because today's tree copy would raw-copy a nested live WAL.
- The hosted deployment is ADR-013's: authentication, tenant resolution at
  every boundary, the row-level policy, migration tooling for its database,
  the amendment to the local-first invariant, and the addition to the
  closed list of cloud dependencies. None of that is started here.
- ADR-009's status line becomes "accepted; extended by ADR-012
  (isolation)". It is not superseded: its decision that configuration is
  data read through accessors stands, and its point 3 explicitly handed
  isolation on. ADR-003 gains the per-store note above.

## Honest limitations

- No second tenant exists, so the local half of the decision is proven only
  by what it refuses to add (a tenant column) and by the direction it
  leaves open, not by two directories side by side.
- The hosted half is a direction with an ADR to write. Naming row-level
  policies here is a constraint on ADR-013, not a design of it.
- Isolation by directory is as strong as the process's choice of
  directory. A process pointed at the wrong `HARRIER_DATA_DIR` reads the
  wrong person. That is ADR-011's class of problem (the rule covers
  harrier's own code only) and is not made worse here.
- One SQLite file per person does not serve many people from one service.
  That is why the hosted path is a different boundary and a different ADR,
  not a bigger version of this one.
- Cross-tenant reporting is not a feature and this ADR makes it harder on
  purpose.
