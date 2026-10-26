---
spec: 168
title: The Python CI gate runs in parallel jobs
status: accepted
approved: yes
milestone: M0
depends: [002, 039, 103]
---

# Spec 168: The Python CI gate runs in parallel jobs

## Problem

The CI workflow's Python job (`check-python` in `.github/workflows/ci.yml`)
runs `just check-python`: ruff, ruff format, pyright, import-linter and
pytest, one after the other, in one job. On run 39491897862 (main,
2026-10-20) the job took 8 min 36 s. Of that, pytest took 455 s ("4766
passed, 31 skipped ... in 455.34s"). Everything before pytest took 16 s.
Every other CI job finished in under 75 s.

So every pull request waits about 8.5 minutes for its Python result, and
the deploy to staging (`deploy.yml`, which waits for CI) waits with it.

Splitting the static checks from pytest alone saves at most those 16 s.
The time is in pytest, so pytest itself has to run in parallel.

pytest-xdist inside one job is not safe here. The Postgres tests create
cluster-wide roles (`harrier_migrator`, `anon`, `authenticated`,
`service_role`, `harrier_tenant`) on one server (`tests/pg_support.py`),
and two workers on the same server race to create them. Separate jobs
each get their own Postgres service, so they share nothing.

## Scope

- `.github/workflows/ci.yml`: the Python job becomes a static job, four
  pytest shard jobs and one gate job.
- `justfile`: `check-python` is split into two recipes it still runs.
- `services/api/tests/conftest.py`: a collection hook that keeps one
  shard's tests.
- `services/api/tests/test_test_shards.py`: new, proves the hook.
- `docs/quality-ci-plan.md`: the gate list says how the Python tests run
  in CI.

The required status checks of the `main-protection` ruleset do not change.

## Behavior

### Shard selection

`HARRIER_TEST_SHARD` selects a shard. Its value is `K/N`, two positive
integers with `1 <= K <= N`.

- **Unset or empty:** every collected test runs, as today. Local
  `just check`, `just gate` and plain `uv run pytest` behave as before.
- **`K/N`:** after collection, the collected tests are numbered from 0 in
  collection order. Test number `i` stays if `i mod N == K - 1`. The others
  are deselected, and pytest's summary counts them as deselected.
- **Any other value** (`0/4`, `5/4`, `2/0`, `x`, `1/`, `1/4/2`): pytest
  stops before running any test, exit code 4 (usage error), with a message
  that names `HARRIER_TEST_SHARD` and the value it got.

So for one collection, the `N` shards together hold every test exactly
once, and their sizes differ by at most one.

### Recipes

- `just check-python-static`: `uv sync`, ruff check, ruff format check,
  pyright, import-linter.
- `just check-python-tests`: `uv sync`, pytest. It passes
  `HARRIER_TEST_SHARD` through from the environment.
- `just check-python` runs both, in that order, as it runs today's steps.
  `just check` is unchanged.

### CI jobs

- **Python static** runs `just check-python-static`. It needs no Postgres
  and no apt packages.
- **Python tests (shard K of 4)**, for K from 1 to 4, a matrix with
  `fail-fast: false`. Each has its own Postgres 17 service, the PostgreSQL
  17 client and age, `HARRIER_TEST_POSTGRES_URL`, and runs
  `just check-python-tests` with `HARRIER_TEST_SHARD=K/4`. `CI=true` still
  makes a missing or unreachable Postgres fail the shard (spec 039).
- **Python (ruff, pyright, import-linter, pytest)**, the name the ruleset
  requires, needs the static job and every shard and runs even when one of
  them failed or was cancelled. It passes only if every job it needs
  succeeded. Otherwise it fails and names the job that did not.

All six jobs start when the workflow starts; only the gate waits.

## Failure modes

- A shard fails: the other shards still finish (`fail-fast: false`), so one
  run shows every failing test. The gate fails.
- A shard is cancelled or skipped: the gate fails. A skipped shard has not
  run, so it has not passed (spec 039).
- A test depends on another test in its file having run first: it may fail
  once the two land in different shards. That is a fault in the test, and
  the fix is in the test, not in the shard rule.
- A malformed `HARRIER_TEST_SHARD`: exit 4 before any test runs, never a
  silent full or empty run.

## Acceptance criteria

- [ ] With `HARRIER_TEST_SHARD` set to `1/3`, `2/3` and `3/3` over the same
      test files, the three collected sets are disjoint and their union is
      the unsharded collection (planned test_shards_partition_the_collection).
- [ ] Shard sizes for one collection differ by at most one (planned
      test_shards_differ_in_size_by_at_most_one).
- [ ] Each malformed value listed under Behavior exits 4 with no test run
      and a message naming `HARRIER_TEST_SHARD` (planned
      test_a_malformed_shard_is_a_usage_error).
- [ ] An empty `HARRIER_TEST_SHARD` collects the same tests as an unset one
      (planned test_an_empty_shard_runs_everything).
- [ ] On the implementing pull request's CI run, the four shards' passed
      counts add up to the passed count of the same commit run unsharded,
      and so do the skipped counts. The pull request states both totals.
- [ ] On the implementing pull request's CI run, the gate job finishes in
      less than half of the baseline's 8 min 36 s, counted from the
      workflow's start. The pull request states the measured time.
- [ ] A pull request where one shard fails shows the gate job failed,
      named as the required check. Proved once on the implementing pull
      request with a deliberately failing commit, then reverted; the
      pull request links that run.
- [ ] `just check` passes locally.

## Proof / origin

- Run 39491897862, job "Python (ruff, pyright, import-linter, pytest)":
  `just check-python` from 20:26:32 to 20:34:27 UTC, pytest's summary line
  at 20:34:25.
- The `main-protection` ruleset (id 20584017) requires the context
  "Python (ruff, pyright, import-linter, pytest)".
- `services/api/tests/pg_support.py`: the cluster-wide roles every
  Postgres test creates.

## Out of scope

- pytest-xdist or any parallelism inside one job.
- Balancing shards by measured test duration. The split is by count.
- Caching uv or apt downloads between runs.
- The TypeScript job and the other CI jobs.
- Changing the ruleset's required checks.

## Honest limitations

- Shards balance by test count, not time. A shard that draws more slow
  Postgres tests finishes last, and the gate waits for it.
- Each shard pays its own setup: container start, apt install, `uv sync`,
  collection, and the session-scoped Postgres template migration. Total
  runner minutes per run go up, while wall-clock time goes down.
- Module-scoped fixtures run once per shard that holds a test of that
  module, so up to four times instead of once.
- The shard count is fixed at four in the workflow. Changing it is a
  workflow edit.

## Migration

None. Locally nothing changes unless `HARRIER_TEST_SHARD` is set.
