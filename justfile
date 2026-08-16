# Harrier task runner. CI calls these same recipes (docs/quality-ci-plan.md).

api_dir := "services/api"

default:
    @just --list

# Start FastAPI and Vite together for development. PID-based cleanup: just runs
# recipes in a non-interactive sh with no job control, so %1 would not work.
# uvicorn runs from the REPO ROOT (uv --project) so run-manager subprocesses
# inherit a cwd where config/, data/, and .env resolve (spec 011).
dev:
    uv run --project {{api_dir}} uvicorn harrier_api.app:app --reload --port 8000 & api_pid=$!; \
    trap 'kill "$api_pid" 2>/dev/null || true' EXIT INT TERM; \
    pnpm --filter @harrier/web dev

# Build and start the supervised container (spec 051, ADR-010). The API and
# the built SPA on http://127.0.0.1:8000, back whenever Docker starts.
#
# The mkdir is load-bearing rather than defensive: compose creates a missing
# bind-mount source as root, which then breaks every write from a container
# running as the host user, and looks like a code fault rather than a
# permission one. The uid and gid are passed for the same reason: the local
# auth token is 0600 in data/.
container-up:
    mkdir -p data config secrets
    HARRIER_UID="$(id -u)" HARRIER_GID="$(id -g)" \
    HARRIER_REVISION="$(git rev-parse --short HEAD)$([ -n "$(git status --porcelain)" ] && echo -dirty)" \
    HARRIER_BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    docker compose up -d --build
    @echo "harrier on http://127.0.0.1:8000"

container-down:
    docker compose down

container-logs:
    docker compose logs -f harrier

# What the running container actually is, which is the question a stale image
# makes worth asking.
container-status:
    @docker compose ps
    @curl -fsS http://127.0.0.1:8000/health || echo "not answering on 127.0.0.1:8000"

# Demo mode for strangers: synthetic fixtures, no keys, and no network in
# the demo itself (spec 021). The two lines below are the build, not the
# demo: pnpm needs the registry on a cold store and vite writes
# apps/web/dist (gitignored) into the checkout. Everything the demo then
# produces goes to a temp directory, never the clone.
demo:
    pnpm install --frozen-lockfile
    pnpm --filter @harrier/web build
    @echo "harrier demo on http://127.0.0.1:8000 (synthetic data, ctrl-c to stop)"
    HARRIER_DEMO=1 uv run --project {{api_dir}} uvicorn harrier_api.app:app --port 8000

# One offline discovery run over the demo fixtures: the real screening
# pipeline, synthetic boards, nothing written into the clone.
demo-discover:
    HARRIER_DEMO=1 uv run --project {{api_dir}} harrier discover

# Full local gate, identical to CI.
check: check-python check-ts contract aie-check spec-structure

check-python:
    cd {{api_dir}} && uv sync --quiet
    cd {{api_dir}} && uv run ruff check .
    cd {{api_dir}} && uv run ruff format --check .
    cd {{api_dir}} && uv run pyright
    cd {{api_dir}} && uv run lint-imports
    cd {{api_dir}} && uv run pytest -q

check-ts:
    pnpm install --frozen-lockfile
    pnpm --filter @harrier/web type-check
    pnpm --filter @harrier/web lint
    pnpm --filter @harrier/web format:check
    pnpm --filter @harrier/web test

# Fast turn-end gate (called by .claude/hooks/verify-on-stop.sh): type-checks + tests.
gate:
    cd {{api_dir}} && uv run pyright
    cd {{api_dir}} && uv run pytest -q
    pnpm --filter @harrier/web type-check
    pnpm --filter @harrier/web test

# Regenerate the API contract artifacts (openapi.json + TS types). CI runs this
# and fails on any git diff (ADR-005).
contract:
    cd {{api_dir}} && uv run python -m harrier_api.export_openapi ../../packages/contract/openapi.json
    pnpm --filter @harrier/contract generate

aie-check:
    npx aie check

# Export tracker data to CSV in the legacy shapes.
export:
    cd {{api_dir}} && uv run harrier export --dest ../../tracker

# Snapshot all local personal data (database, state, artifacts) to a timestamped
# archive OUTSIDE the repo (ADR-008: backup is entirely local).
backup:
    ./scripts/backup.sh

spec-structure:
    python3 scripts/check_spec_structure.py specs
