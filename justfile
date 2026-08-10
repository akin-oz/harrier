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

# Demo mode for strangers: synthetic fixtures, no keys, no network, and
# nothing written into the clone (spec 021). Builds the SPA, then serves it
# and the API from one origin against a throwaway database in a temp dir.
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
check: check-python check-ts contract aie-check

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
