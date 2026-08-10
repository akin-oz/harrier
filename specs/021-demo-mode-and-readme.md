---
spec: 021
title: Demo mode, fixtures, public README
status: in-progress
approved: yes
milestone: M5
depends: [005,006]
---

# Spec 021: Demo mode, fixtures, public README

## Problem

A stranger clones, runs one command, and sees the system work on synthetic
data. Today `just demo` prints that spec 021 will implement it and exits 1.

Three things stand between the repo and that: none of the never-in-git
config files exist on a stranger's machine, every source reaches the
network, and the README describes a project that is not yet runnable.

## Scope

- harrier.demo: the demo switch (HARRIER_DEMO=1) and the substitutions it
  makes, which are exactly two.
  - resolve_config_path: a never-in-git config file reads from its
    committed `.example` sibling. In demo mode the example wins even when
    the real file exists, because a demo must show the same thing on every
    machine and the owner's watchlist is itself personal data (ADR-009).
  - offline HTTP: request_text serves from fixtures/http/ through an
    explicit url -> filename index. A URL with no fixture raises
    OfflineFixtureError instead of falling through to a request, which is
    what makes "the demo touches no network" provable rather than claimed.
    The lookup sits after the url_allowed check, so demo mode cannot widen
    what a URL may reach.
  - anchored_path: committed files resolve against the repo root when the
    working directory is not it. Not demo-specific; demo mode exposed it.
- data location: under HARRIER_DEMO, data_dir() is a temp directory, so a
  demo run writes nothing into the clone it was launched from.
- seeding: the demo database is created per boot from
  fixtures/demo-jobs.json plus every config/*.example.* document, so the
  resume, cover letter, outreach, and offer surfaces have real-shaped
  input. The seeded persona is fictional and is the same file the tests
  use, so a broken example breaks CI.
- fixtures/http/: synthetic Greenhouse, Ashby, Lever, and RemoteOK
  responses matching the boards in config/feeds.example.txt, carrying a
  mix that exercises the policy: remote EMEA roles that pass, a hybrid
  role, and off-target titles.
- Apify is skipped in demo mode. It is the one paid source and it reaches
  the network outside the fixture seam, so reporting a missing token as an
  error would read to a stranger as a broken clone.
- serving: the API mounts the built SPA at / when apps/web/dist exists,
  and an ASGI middleware strips a leading /api so one origin serves both.
  The web app calls /api/... in every mode; in development Vite rewrites
  the prefix, and when the API serves the SPA there is no proxy to do it.
  One router set means the OpenAPI document, and the generated client,
  stay byte-identical (ADR-005).
- just demo: build the SPA, then serve it and the API on :8000 with
  HARRIER_DEMO=1. just demo-discover runs one offline discovery pass.
- README: what it is, what it does, the architecture and the four rules
  that hold it, specs as the unit of change, and honest limitations.
- the mechanical half of the pre-publish privacy checklist, as tests over
  every committed fixture and example.

## Inputs, outputs, failure modes

- Inputs: HARRIER_DEMO, optionally HARRIER_HTTP_FIXTURES (a directory) and
  HARRIER_DEMO_FIXTURE (a jobs file), the committed examples, fixtures/.
- Outputs: a seeded database in a temp directory, a served SPA, and a
  discovery summary over the fixture boards.
- Failure modes: a URL with no fixture raises OfflineFixtureError naming
  the URL; a fixture index entry that is not a plain filename is refused
  before any read; a missing index while fixtures are enabled raises
  rather than silently going online; an absent apps/web/dist leaves the
  API serving the API alone.

## Stated changes from the old code

There is no old code here. The old repo had no demo path, and its
`config/feeds.txt` and candidate profile were committed with real
contents, which is the thing this spec exists to make impossible to need.

Two deviations from the stub's wording:

- The stub said "recorded-then-scrubbed importer responses". The fixtures
  are authored instead. Recording a real board and scrubbing it afterwards
  makes privacy a review step that can be skipped; authoring means no real
  company was ever in the file. Cost: the fixtures pin the shapes the
  importers parse, not whatever a live board returns today.
- The stub said "external services stubbed". Only HTTP through the
  screening seam is stubbed. LLM providers, Gmail, and Telegram are not:
  they are already inert without credentials, and the demo asserts it
  needs no keys rather than faking the presence of any.

## Acceptance criteria

- [ ] demo mode reads the committed example even when the real config
      file exists, and leaves the path untouched outside demo mode
      (test_demo_mode_reads_the_committed_example_even_when_a_real_config_exists)
- [ ] the demo resolves its config from the repo regardless of the
      working directory
      (test_demo_feeds_resolve_from_the_repo_regardless_of_working_directory)
- [ ] a demo run writes nothing into the clone
      (test_demo_writes_nothing_into_the_clone)
- [ ] a URL with no fixture raises instead of reaching the network, and a
      fixture index entry cannot escape the fixture directory
      (test_unfixtured_url_raises_instead_of_reaching_network,
      test_fixture_entry_cannot_escape_the_fixture_directory)
- [ ] discovery runs offline over the fixture boards, screens them, and
      needs no environment keys; Apify is not attempted
      (test_demo_discovery_runs_offline_and_screens_the_fixture_boards,
      test_demo_discovery_needs_no_environment_keys)
- [ ] the seeded database carries both jobs and every profile document
      (test_seed_demo_db_fills_jobs_and_profile_documents,
      test_profile_seeds_all_name_a_committed_example)
- [ ] one origin serves the SPA and answers under /api, and an unbuilt SPA
      still leaves the API working
      (test_api_serves_the_spa_and_still_answers_under_the_api_prefix,
      test_api_without_a_built_spa_still_serves_the_api)
- [ ] every committed fixture and example names only reserved or ATS
      hosts, no real board slug, and no address outside the example
      domains (test_fixtures_name_only_reserved_or_ats_hosts,
      test_real_ats_hosts_carry_only_example_board_names,
      test_fixtures_contain_no_address_outside_the_example_domains)
- [ ] clean-machine clone-to-demo works with zero keys and no decryption
- [ ] the agent-executable part of the pre-publish checklist
      (docs/privacy-plan.md) is green, and the parts only a human can do
      are named as open
- [ ] All gates green on PR

## Proof / origin

docs/privacy-plan.md; docs/adr/ADR-009-user-configuration-and-tenancy.md.
Proving file: services/api/tests/test_demo.py.

Honest limitations: "no network" is proven for HTTP that goes through
harrier.screening.http, which is every ATS and RemoteOK fetch. Apify
builds its own requests and is skipped in demo mode rather than fixtured,
so its client is not covered by that proof. The privacy pass is
mechanical: it catches hosts, board slugs, and addresses, and cannot
judge whether a synthetic persona resembles a real person, which stays a
human review step.

## Out of scope

The license file, the pre-publish human review, and flipping repository
visibility. Demo coverage of the GUI surfaces that do not exist yet
(spec 022 and later). Seeding contacts and outreach state, which would
need a synthetic contact set that the outreach specs do not yet have.
