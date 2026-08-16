# syntax=docker/dockerfile:1
#
# One image for the API and the built SPA (spec 051, ADR-010).
#
# One container rather than two, because the API already serves
# apps/web/dist at / when the directory exists
# (services/api/src/harrier_api/app.py, `create_app`). That is how `just demo`
# serves the whole product on port 8000 alone, so a second web container and a
# reverse proxy would be a stack where the code needs a process.
#
# The source tree keeps its repository layout inside the image. `repo_root()`
# resolves four parents up from services/api/src/harrier/paths.py and
# `data_dir()` is anchored to it, so placing the tree at /app is what makes
# /app/data and /app/config resolve. Flattening it and setting HARRIER_DATA_DIR
# would move the data directory and leave config/ resolving somewhere else.

# --- the SPA -----------------------------------------------------------------
FROM node:22-bookworm-slim AS web
WORKDIR /build
RUN corepack enable

# Manifests first so a source edit does not re-resolve the dependency graph.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/
COPY packages/contract/package.json packages/contract/
RUN --mount=type=cache,target=/pnpm-store \
    pnpm config set store-dir /pnpm-store && pnpm install --frozen-lockfile

COPY packages/contract packages/contract
COPY apps/web apps/web
RUN pnpm --filter @harrier/web build

# --- the service -------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /usr/local/bin/uv

WORKDIR /app

# `license-files = ["../../LICENSE"]` in the project metadata resolves to
# /app/LICENSE, so the build fails without this line.
COPY LICENSE ./LICENSE
COPY services/api/pyproject.toml services/api/uv.lock services/api/

# Dependencies as their own layer: the project itself is installed after the
# source arrives, so editing a module does not reinstall FastAPI.
RUN --mount=type=cache,target=/root/.cache/uv \
    cd services/api && uv sync --frozen --no-install-project --no-dev

COPY services/api services/api

# Everything the runtime reads from the repository root, not just the code.
# The first version of this image copied services/api, config and the SPA, and
# `harrier tailor` died on a missing resume template inside a running
# container: the code was all there and the assets it reads were not.
# `test_the_image_copies_every_repository_asset_the_runtime_reads` derives this
# list from the source rather than trusting this comment.
#
#   config     discovery settings, outreach templates, the classification
#   templates  resume and cover letter HTML and CSS (harrier.resume,
#              harrier.apply.letters)
#   fixtures   demo mode's offline HTTP recordings and synthetic jobs
#   docs       the parity matrix and its checklist, read by `harrier parity`
#
# data/ and secrets/ are deliberately absent: they are personal and arrive as
# bind mounts at runtime (ADR-008, spec 051).
COPY config config
COPY templates templates
COPY fixtures fixtures
COPY docs docs
COPY --from=web /build/apps/web/dist apps/web/dist

RUN --mount=type=cache,target=/root/.cache/uv \
    cd services/api && uv sync --frozen --no-dev

# What this image is, readable at runtime. An image is stale until rebuilt, so
# a container that answers correctly while running old code is the failure this
# change exists to remove rather than reproduce (spec 051). `docker compose
# build` passes these; a build without them reports "unknown" rather than
# claiming a revision it does not have.
ARG HARRIER_REVISION=unknown
ARG HARRIER_BUILT_AT=unknown
ENV HARRIER_REVISION=${HARRIER_REVISION} \
    HARRIER_BUILT_AT=${HARRIER_BUILT_AT} \
    PATH="/app/services/api/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

# No USER here. The uid is pinned at runtime by the compose file to the host
# user's, because the local auth token is written 0600 into the bind-mounted
# data directory and a uid mismatch makes every state-changing request fail on
# a file permission that does not look like one from the browser (spec 051).

EXPOSE 8000

# 0.0.0.0 is the container's interface, not an exposure decision: the compose
# file publishes to 127.0.0.1 only. TrustedHostMiddleware already allows
# localhost, 127.0.0.1, [::1] and 0.0.0.0, so reaching this from the host
# browser passes and any other hostname gets a 400 (localauth.TRUSTED_HOSTS).
CMD ["uvicorn", "harrier_api.app:app", "--host", "0.0.0.0", "--port", "8000"]
