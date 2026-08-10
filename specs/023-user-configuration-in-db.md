---
spec: 023
title: User configuration in the database
status: shipped
approved: yes
milestone: M5
depends: [004, 011]
---

# Spec 023: User configuration in the database

## Problem

User configuration (the board watchlist, the LinkedIn searches, the
discovery settings, the hold list) lives in gitignored loose files. ADR-009
wants it in the database: customization without editing a checkout, a clean
open-source story, and a data layer a tenant scope can partition later.

## Scope

- `user_config` table: one row per (scope, kind), value as JSON. The four
  kinds are feeds, linkedin_searches, discovery, company_holds.
- `scope` is the tenancy seam. It is `default` everywhere today and nothing
  reads it as a variable, but it is in the unique key, so partitioning later
  is a query change rather than a migration of every row (ADR-009: tenant
  ready, not tenant complete).
- Shape validation on both the write and the read path. Write, so a bad
  value surfaces where it was set rather than inside discovery. Read,
  because a row can appear without going through the write path at all: a
  hand-edited database, a restored backup, a future migration.
- `harrier.userconfig` accessors resolving store, then file, then empty.
  Every accessor takes an optional connection; None means "no store here",
  which is how file-based callers and every test predating this spec keep
  working unchanged.
- Discovery reads feeds, searches, the hold list, and the Apify count
  through those accessors.
- CLI: `harrier config list | get | set | unset | import`.
- API: `GET /config`, `GET|PUT|DELETE /config/{kind}`, each answer carrying
  whether the value came from the store or a file.

## Inputs, outputs, failure modes

- Inputs: the `user_config` table; `config/feeds.txt`,
  `config/linkedin_search_urls.txt`, `config/discovery.json`,
  `config/companies-hold.csv` as import sources and fallbacks.
- Outputs: stored configuration, and what discovery reads.
- Failure modes: a value of the wrong shape raises `ConfigError` naming
  the kind and the shape expected, which the API surfaces as 400 and the
  CLI as a non-zero exit; an unknown kind is 404 on the API and a non-zero
  exit on the CLI; stored JSON that no longer matches its kind raises on
  read rather than being coerced.

  400 rather than 422 for a bad value, because FastAPI already owns 422 for
  a malformed request body, where the detail is a list of field errors.
  Reusing it would put two shapes behind one status and hide the automatic
  one from the generated contract entirely.

## Resolution order, and why the file stays

1. The store, when a row exists for the scope.
2. The file, resolved through `harrier.demo.resolve_config_path` so demo
   mode still gets its synthetic values.
3. Empty.

Step 2 is what lets this ship without the migration being mandatory: an
existing install keeps running before `harrier config import`, and a fresh
clone with neither files nor rows runs cleanly with no sources, which is a
clean state rather than an error.

An empty list and no row are deliberately different answers. No row means
fall back to the file; an empty list means the watchlist was cleared on
purpose, and a user who empties it must not silently get the file back.

## Stated changes from the old code

- The hold list had two loaders after the accessors landed, one in
  `harrier.screening.config` and one in `harrier.userconfig`. The
  screening one is deleted rather than kept as a wrapper: two loaders that
  can drift is the duplication the parity matrix flags repeatedly in the
  old repo.
- The hold list's `reason` column is dropped on import. Nothing reads it,
  and it is personal operational commentary about companies (ADR-008).
- `parse_ats_feeds` is split: `route_ats_feeds` groups URLs by importer, and
  the file reader wraps it. The routing is the part configuration needs.
- Two parity-matrix rows move from keep to change, because their storage
  moved even though their behavior did not. Counts corrected to 58/22/16.

## Acceptance criteria

- [x] discovery reads feeds and searches from the store, and a fresh clone
      with no files and no store rows runs cleanly with empty sources
      (test_stored_feeds_route_to_their_importers,
      test_a_fresh_install_with_no_store_and_no_files_runs_with_no_sources)
- [x] the file is used until something is stored, and clearing a value is
      distinguishable from never setting one
      (test_the_file_is_used_until_something_is_stored,
      test_an_empty_list_is_not_the_same_as_no_row)
- [x] a bad shape or unknown kind is refused at the write, by both surfaces
      with the same message (test_a_bad_shape_is_refused_at_the_write,
      test_the_api_refuses_a_bad_shape_with_the_stores_own_message,
      test_an_unknown_kind_is_a_404_on_every_verb)
- [x] config is editable through the API and the CLI, and every answer says
      where the value came from
      (test_the_api_lists_every_kind_with_its_source,
      test_putting_a_value_makes_it_the_stored_source,
      test_deleting_a_value_restores_the_fallback,
      test_unset_reports_whether_anything_was_removed)
- [x] the import command round-trips the current files, ignoring the
      example file's reader-facing comment key
      (test_import_round_trips_the_current_files,
      test_import_with_no_files_reports_rather_than_claiming_success)
- [x] the schema carries a scope column that partitions
      (test_the_schema_carries_a_scope_column_for_later_tenancy)
- [x] the discovery settings survive the move, including the boolean trap
      (test_discovery_settings_come_from_the_store,
      test_a_boolean_count_does_not_pass_as_an_integer)
- [x] a corrupted row is refused on read rather than coerced, and a
      malformed request body stays a distinct failure from a bad value
      (test_a_corrupted_row_is_refused_rather_than_coerced,
      test_a_malformed_body_and_a_bad_value_are_different_failures)
- [x] All gates green: `just check` (ruff, ruff-format, pyright,
      lint-imports, pytest; tsc, eslint, prettier, vitest; contract
      regeneration; aie check), run by the CI workflow. History: PR #20.

## Proof / origin

docs/adr/ADR-009-user-configuration-and-tenancy.md; ADR-008; the spec 011
review thread that prompted the reclassification. Proving file:
services/api/tests/test_userconfig.py.

The reproducible proof of the round trip is
`test_import_round_trips_the_current_files`, which builds its own config
tree from committed inputs.

Separately, and not reproducible from this repository: a manual local check
ran `harrier config import` against the author's actual gitignored
`config/` tree and round-tripped 414 feed URLs (55 Greenhouse, 340 Ashby,
19 Lever) plus the Apify scheduled count into a scratch store, leaving the
real database untouched. Those counts are environment-specific and no other
clone can reproduce them; they are recorded as evidence the import handles
a real watchlist at size, not as a checkable criterion.

Honest limitations: the scope column exists and partitions, which is not
the same as multi-tenancy. There is no authentication, no tenant
resolution, and no isolation; a second scope is reachable only by passing
it explicitly in process. The API write path has no auth either, because
the service binds to localhost (unchanged from every other endpoint). The
GUI half of ADR-009's promise is not here: the React app has no
configuration surface yet, so "customizable easily" currently means the
CLI and the API, not a settings page.

## Out of scope

Authentication, tenant isolation, hosting (a future multi-tenant ADR).
The GUI configuration surface, which needs a page that does not exist yet
and belongs with the other surface specs. Moving the candidate profile or
the resume content, which are profile documents rather than configuration
and already live in the database (spec 004).
