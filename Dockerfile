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
# Every non-dev dependency group, because the UI can reach every feature.
# Optional on a laptop means "install it when you want that feature"; in an
# image it means the feature is missing and the operator cannot install it.
# `pdf` backs resume and cover letter generation, whose artifact gate is
# PDF-or-failure. `gmail` backs the mail watch run that `POST /mail/watch`
# starts. Both were absent from the first image and both fail identically:
# a run that dies on a lazy import telling the operator to run an install
# command, inside a container.
RUN --mount=type=cache,target=/root/.cache/uv \
    cd services/api && uv sync --frozen --no-install-project --no-dev --group pdf --group gmail

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
    cd services/api && uv sync --frozen --no-dev --group pdf --group gmail

# Chromium renders the PDF and `pdfinfo` counts its pages. Both are needed for
# `harrier tailor` to reach a validated artifact rather than an error.
#
# The browser path is pinned rather than left to default. The container runs as
# the host user's uid with no passwd entry, so HOME is unset and resolves to
# `/`, and Playwright looked for its browser under `/.cache/ms-playwright`,
# which is neither writable at build time nor where the download went. A fixed
# world-readable location makes the lookup independent of which uid runs it.
#
# This is the largest thing in the image by far. It is here because the
# alternative is an artifact run that fails in the UI with a message telling
# the operator to run an install command inside a container.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN /app/services/api/.venv/bin/playwright install --with-deps chromium \
    && apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /ms-playwright

# The `claude` CLI, for the same reason as Chromium above: AI_PROVIDER selects
# it, the UI can reach every feature that calls it, and the container could not
# run it. `.env` carries AI_PROVIDER=claude-cli to the container through
# `env_file`, so resume tailoring, cover letters, application answers, outreach
# drafts and offer evaluation all failed identically with "`claude` CLI not
# found", which is the fourth round of this defect and the first one that could
# not be fixed by installing a Python group.
#
# Installed under a fixed world-readable prefix rather than a home directory,
# for the reason PLAYWRIGHT_BROWSERS_PATH is pinned: the container runs as the
# host user's uid with no passwd entry, so HOME resolves to `/`. The installer
# places the binary under $HOME, so the build sets HOME for that command only
# and tells the runtime where it landed. CLAUDE_CLI_PATH is the override
# harrier.llm.config.find_binary reads first, so nothing here depends on PATH.
#
# The version is pinned. A floating `stable` would mean the image's behavior
# changes without the tree changing, which is the staleness failure this spec
# exists to make visible, inverted.
#
# curl is installed for this and then purged, so the compose healthcheck's
# reason for using python rather than curl stays true.
#
# The download is a file, not a pipe, and the layer verifies what it produced.
# `curl ... | bash` returns the shell's status rather than curl's, so a failed
# download fed an empty script to a shell that exited 0: the layer went green
# and the image shipped without the CLI. That was reproduced rather than
# reasoned about, on the first version of this block (review of PR #59), and it
# is the "reports success while doing nothing" shape this repository keeps
# finding. So the last two clauses are the guard: the binary must exist, be
# executable, and answer with the version that was pinned. An image that lacks
# the CLI now fails to build instead of failing at runtime in front of the
# operator.
ARG CLAUDE_CLI_VERSION=2.1.224
ENV CLAUDE_CLI_PATH=/opt/claude/.local/bin/claude
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL --retry 5 --retry-all-errors --http1.1 \
        https://claude.ai/install.sh -o /tmp/install-claude.sh \
    && HOME=/opt/claude bash /tmp/install-claude.sh "${CLAUDE_CLI_VERSION}" \
    && rm -f /tmp/install-claude.sh \
    && chmod -R a+rX /opt/claude \
    && test -x "${CLAUDE_CLI_PATH}" \
    && test "$("${CLAUDE_CLI_PATH}" --version | cut -d' ' -f1)" = "${CLAUDE_CLI_VERSION}" \
    && apt-get purge -y --auto-remove curl \
    && rm -rf /var/lib/apt/lists/*

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
