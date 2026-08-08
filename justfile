# Harrier task runner. CI calls these same recipes (docs/quality-ci-plan.md).

api_dir := "services/api"

default:
    @just --list

# Start FastAPI and Vite together for development. Real API arrives with spec 005.
dev:
    @echo "dev: FastAPI service lands with spec 005; starting the web app only."
    pnpm --filter @harrier/web dev

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

# Regenerate the API contract and fail on drift. Real generation arrives with spec 005.
contract:
    @echo "contract: no API routes yet; generation lands with spec 005."

aie-check:
    npx aie check

# Decrypt private data into private/decrypted/ (gitignored). Requires the age key.
decrypt:
    ./scripts/decrypt.sh

# Export tracker data to CSV. Implemented by spec 004.
export:
    @echo "export: implemented by spec 004 (SQLite to CSV)." && exit 1
