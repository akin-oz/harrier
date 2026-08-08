# Harrier task runner. CI calls these same recipes (docs/quality-ci-plan.md).

api_dir := "services/api"

default:
    @just --list

# Start FastAPI and Vite together for development.
dev:
    (cd {{api_dir}} && uv run uvicorn harrier_api.app:app --reload --port 8000) & \
    pnpm --filter @harrier/web dev; kill %1 2>/dev/null || true

# Demo mode for strangers: seeded fixtures, no secrets. Implemented by spec 021.
demo:
    @echo "demo: implemented by spec 021 (fixtures + API serving the built SPA)." && exit 1

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
