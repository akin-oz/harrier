---
spec: 051
title: The API and UI stay up without being started by hand
status: accepted
approved: yes
milestone: M8
depends: [006, 011, 020, 021, 035, 038]
---

# Spec 051: The API and UI stay up without being started by hand

## Problem

The daily driver is started by hand, in a foreground shell, with nothing
supervising it. That produced a failure worth writing down, because every part
of it reported success.

`just dev` printed `ERROR: [Errno 48] Address already in use` and kept going.
Port 8000 was held by an earlier uvicorn whose shell had exited, leaving it
reparented to init. That process answered `GET /health` with 200, so the Vite
dev server came up, proxied to it, and the UI looked correct. It had been
started without `--reload`, so it was serving an older revision of the code.
Nothing anywhere said so.

Three properties combined:

1. **Nothing supervises the API.** It runs while someone remembers to run it,
   and stops when a terminal closes or the machine sleeps and wakes badly.
2. **A stale server is indistinguishable from a fresh one at the port.** The
   only signal was a single error line above a working UI, and the health
   endpoint cannot tell the operator which revision answered it.
3. **The dev recipe's cleanup does not fire.** `dev` in the justfile captures
   `$!` after backgrounding uvicorn and traps on that PID. When uvicorn exits
   immediately on a bind failure, the trap holds a dead PID, so Ctrl-C tears
   down nothing and the orphan survives the next attempt too.

This is the operability failure class ADR-006 already names in another form:
a component that stops running without anything reporting it. There the answer
was to keep the scheduler out of the API's lifetime. Here the answer is to give
the API a lifetime that does not depend on a human and a terminal tab.

Affects one operator, on one machine, every day.

## Scope

### One image, one service

The API already serves the built SPA: `create_app` mounts `apps/web/dist` at
`/` as `StaticFiles(html=True)` when the directory exists
(`services/api/src/harrier_api/app.py:721`), which is how `just demo` serves the
whole product on port 8000 alone. So this is one container, not two. No second
web container, no reverse proxy, no nginx.

The image builds the SPA and installs the Python service. The container runs
uvicorn against `harrier_api.app:app`.

### The container is a peer of the host, not a replacement for it

launchd stays on the host, calling the host CLI, exactly as ADR-006 decided and
spec 020 built. Nothing about the schedule moves. The container owns the API
and the UI, which is the part that today has no supervisor. This spec does not
reopen ADR-006.

It does contradict one sentence in ADR-007: "Docker remains only for the demo
path (optional), not the daily driver". That line is superseded by ADR-010,
which lands with this spec. The spec does not proceed without it, because
implementing against a live ADR that forbids the change is the failure the
governance rules exist to prevent.

### What is published, and where

`127.0.0.1:8000` only. Never `0.0.0.0` on the host side. Spec 035 spent its
scope establishing that this API is not an open door, and a published container
port is the easiest way to undo that without noticing.

`TrustedHostMiddleware` is already configured with
`("localhost", "127.0.0.1", "[::1]", "0.0.0.0", "testserver")`
(`services/api/src/harrier_api/localauth.py:54`), so reaching the container as
`localhost:8000` or `127.0.0.1:8000` passes and any other hostname gets a 400.
That is the correct behavior and this spec does not widen the list.

### Restart policy

`restart: unless-stopped`. The container returns whenever the Docker daemon
starts, including after a reboot, and stays down when it was stopped
deliberately. Making that mean "live all the time" also requires Docker Desktop's
own "Start Docker Desktop when you sign in" setting, which is machine
configuration rather than repository state. The README says so rather than the
repository pretending it can guarantee it.

### What is mounted

The container holds no personal data of its own. Everything personal stays on
the host and is bind-mounted, which keeps ADR-008 intact and keeps the container
disposable.

| Host path | Container path | Mode | Why |
|---|---|---|---|
| `./data` | `/app/data` | read-write | The tracker database, state, logs, and the local auth token (ADR-003, ADR-008). |
| `./config` | `/app/config` | read-write | Discovery settings and the profile documents the UI writes. |
| `./secrets` | `/app/secrets` | read-only | Gmail OAuth client and token. The container reads them and must never rewrite them. |
| `.env` | environment | via `env_file` | Credentials, never copied into the image. |

`repo_root()` resolves four parents up from
`services/api/src/harrier/paths.py`, and `data_dir()` is anchored to it
(`services/api/src/harrier/db.py:19`). So the image must place the source tree at
`/app/services/api/src/...` for `/app/data` and `/app/config` to resolve. The
alternative, setting `HARRIER_DATA_DIR`, moves only the data directory and leaves
`config/` resolving somewhere else. Keeping the layout is one decision instead of
several.

### Two writers on one SQLite file

This is the part of the change most likely to cause real damage, so it is
scoped rather than discovered later.

After this change, two processes write `data/harrier.db`: the container, and the
host CLI that launchd invokes on the cadence in `config/schedule.json`. ADR-003
says one write path, and both do go through it, but they are now separate
processes reaching the same file across a Docker bind mount. File locking over
macOS bind mounts is not reliable in the way SQLite's default locking assumes,
and the failure is corruption rather than an error.

The implementation picks one of these and the spec is amended to record which:

- **Host stays authoritative, container connects out.** The container reaches
  the database over the host rather than through the mount. Highest change cost.
- **launchd invokes the container.** Scheduled jobs become `docker exec` into
  the running container, so one process writes through one mount. This keeps a
  single writer, at the cost of making the schedule depend on the container
  being up, which is close to what ADR-006 rejected for the API. It is not the
  same thing (launchd still owns the timing and the retries) but it is adjacent
  enough to state plainly.
- **WAL plus a single-writer lock.** Cheapest, and the one that relies on the
  mount's locking behaving. Only acceptable with a test that demonstrates
  concurrent writes across the boundary, not an assumption that it is fine.

An implementation that picks none of these and simply mounts the file is out of
bounds under this spec.

**Amendment, at implementation.** The third was taken: WAL, which `connect`
already set, plus a busy timeout, which it did not. The first option turned out
not to describe anything buildable, since the container is the API and there is
no second process for it to reach the database through. The second contradicts
ADR-010's stated consequence that the schedule keeps running when Docker is
down, so taking it would have meant reopening the ADR rather than implementing
it.

The condition attached to the third option is met rather than waived. Without
a busy timeout two processes writing at once fail with "database is locked",
executed and observed, not reasoned about; with it both commit and
`PRAGMA integrity_check` returns ok. The timeout is finite so a genuinely stuck
writer still surfaces as an error instead of hanging a scheduled run forever,
which is its own case in the same test file
(`services/api/tests/test_concurrent_writers.py`).

Honest limitation: that test exercises two processes against one file, which is
the mechanism, on whatever filesystem the test runs on. It does not by itself
prove Docker Desktop's bind mount honours the same locking. That was verified
by hand on this machine and is not pinned by CI, because pinning it needs a
container in the Python job.

### Staleness must be legible

The bug this spec exists to prevent is a server that answers correctly while
running old code. A container makes that easier, not harder: an image is stale
by construction until rebuilt.

So the container reports what it is running. The API exposes the built revision
and the build timestamp on its existing health path, and the UI shows it. A
running container whose image predates the working tree is then visible instead
of silent. Without this criterion the change re-creates today's failure with
better ergonomics, which is worse than leaving it alone.

### The image carries what the features need, not only what imports resolve

**Amendment, at implementation.** This spec said the image "builds the SPA and
installs the Python service", and that turned out to describe an image that
cannot do the work. Three rounds of the same defect:

1. `templates/`, `fixtures/` and the parity documents were never copied, so
   every module was present and the files three of them open were not.
   `harrier tailor` died on a missing resume template.
2. The `pdf` dependency group was skipped, because it is optional. Optional on
   a laptop means "install it when you want the feature"; in an image it means
   the feature is absent and the operator cannot add it. The next run died on
   a missing Chromium.
3. `gmail` was the same shape behind it, unhit only because nobody had started
   a mail watch from the UI yet.

So the rule is stated rather than left to whoever edits the Dockerfile next:
**the image installs every non-dev dependency group and copies every
repository directory the runtime resolves.** Both halves are derived by tests
from the source and from `pyproject.toml`, not from a list in the Dockerfile,
because a hand-maintained list is what produced all three:

- `services/api/tests/test_container.py::test_the_image_copies_every_repository_asset_the_runtime_reads`
- `services/api/tests/test_container.py::test_the_image_installs_every_feature_dependency_group`
- `services/api/tests/test_container.py::test_the_dev_group_stays_out_of_the_runtime_image`

Chromium and `poppler-utils` are in the image for the same reason. The artifact
gate is PDF-or-failure and the page count comes from `pdfinfo`, so a container
without them turns every artifact run into an error telling the operator to run
an install command inside a container.

**Second amendment, at implementation.** A fourth round, and the first that the
rule above does not cover, because the missing thing was neither a dependency
group nor a repository directory.

`.env` sets `AI_PROVIDER=claude-cli` and `env_file` hands it to the container,
which had no `claude` binary, no Node, and `HOME` resolving to `/`. Every
feature behind the LLM seam failed with "`claude` CLI not found": resume
tailoring, cover letters, application answers, outreach drafts, and offer
evaluation, all six call sites of `harrier.llm.generate_text`. So the rule
widens by one clause: **the image also carries the external binaries the
configured provider seam resolves.**

The image installs the CLI at a pinned version under `/opt/claude`, and sets
`CLAUDE_CLI_PATH`, which is the override `find_binary` reads before `PATH`
(`services/api/src/harrier/llm/config.py:96`). A fixed world-readable prefix
rather than a home directory, for the same reason `PLAYWRIGHT_BROWSERS_PATH` is
pinned.

**What the container cannot have, stated rather than discovered.** The host's
`claude-cli` runs on a Max subscription whose OAuth credential lives in the
macOS Keychain, not in a file, and the host binary is a Mach-O arm64
executable. Neither crosses into a Linux container. So the container's CLI
authenticates with `ANTHROPIC_API_KEY` instead, through the
`CLAUDE_CLI_USE_API_KEY` switch the provider already reads
(`services/api/src/harrier/llm/providers.py:157`), set in the compose file.

The consequence is a cost asymmetry, and it belongs in the spec rather than in
a surprised invoice: **an AI run started from the container is billed per
token; the same run started from the host CLI is not.** Carrying the
subscription in would mean exporting a live OAuth token from the Keychain to a
bind-mounted file, where two independent refreshers could rotate each other
out of a session. That was rejected, and is recorded here so the next reader
sees a decision rather than an oversight.

Verified rather than reasoned about, on this machine, before the Dockerfile was
edited: the pinned installer produces a working `linux/arm64` binary, it runs as
uid 501 with `HOME=/`, every flag `_generate_claude_cli` passes is still
accepted, and the call returns the `{"is_error": false, "result": ...}` envelope
the provider parses.

Cost, stated rather than discovered: the image is about 2.8 GB. Chromium with
its system libraries is most of it, and the `claude` CLI added by the second
amendment below is 279 MB more. That is the price of the daily driver being
able to do what the host could.

`PLAYWRIGHT_BROWSERS_PATH` is pinned. The container runs as the host user's uid
with no passwd entry, so `HOME` resolves to `/` and Playwright looked for its
browser under `/.cache/ms-playwright`, which is not where the build put it.
Proven by
`services/api/tests/test_container.py::test_the_browser_path_is_pinned_rather_than_left_to_home`,
which asserts the install commands rather than the comments that explain them.

## Inputs, outputs, failure modes

- Inputs: `docker compose up -d` from the repository root, the host's `.env`,
  and the bind-mounted host directories.
- Outputs: the same API and the same UI on `http://127.0.0.1:8000`, unchanged
  in shape. No route changes. No contract change, so `just contract` produces
  no diff.

Failure modes that must reach the operator:

- **Port 8000 is already held** by a host uvicorn, which is the situation this
  spec starts from. Compose fails to bind and says so. The README says how to
  find the holder, because `lsof -nP -iTCP:8000 -sTCP:LISTEN` is the step that
  was missing when this happened.
- **The auth token is unreadable across the boundary.** `load_or_create_token`
  writes `data/<token>` at 0600 (`localauth.py:61`). A container running as a
  different uid than the host user either cannot read a token the host created,
  or creates one the host CLI cannot read. Every state-changing request then
  fails on a file permission, which does not look like a permission problem from
  the browser. The compose file pins the container's uid to the host user's, and
  a test asserts the token written by one is readable by the other.
- **`secrets/` is mounted read-only and the Gmail token needs refreshing.** The
  refresh fails inside the container. `gmail-oauth` is a host CLI operation
  (spec 050 lists it as CLI-only for exactly this kind of reason), so the
  container must fail loudly rather than silently running without mail watch.
- **The image is stale.** Covered above. Visible on the health path and in the UI.
- **A bind mount is missing** because the directory does not exist on a fresh
  clone. Docker creates it as root-owned, which then breaks writes in a way that
  looks like a code bug. The compose path must not rely on Docker creating
  directories.
- **Demo mode is unaffected.** `HARRIER_DEMO=1` writes to a temp directory
  (`db.py:24`) and must keep doing so inside a container, never into a mount.

Failure modes this must not introduce:

- The API reachable from outside the loopback interface.
- Personal data copied into an image layer, which would survive `docker image
  save` and outlive any deletion of the mounted directory.
- Credentials baked into the image or written into a committed compose file.
- Two concurrent writers to the tracker database, per the section above.
- Scheduled runs that depend on the container being up, unless the implementation
  deliberately chooses the `docker exec` option and this spec is amended to say so.

## Acceptance criteria

Proving symbols are named at implementation.

- [x] ADR-010 is accepted and supersedes the "not the daily driver" sentence in
      ADR-007; ADR-007 carries the revision marker the way ADR-002 carries
      ADR-008's
- [x] `docker compose up -d` from a clean clone with a populated `.env` serves
      the same UI on `http://127.0.0.1:8000` that `just demo` serves
- [x] the published port is bound to `127.0.0.1` and a test asserts the compose
      file declares no unqualified port mapping
- [x] `restart: unless-stopped` is declared, and stopping the container
      deliberately leaves it stopped across a Docker daemon restart
- [x] no secret value appears in the Dockerfile, the compose file, or any image
      layer, asserted by a test that greps the built image's history and the
      committed files
- [x] `data/`, `config/` and `secrets/` are bind mounts, `secrets/` is read-only,
      and a test asserts nothing under those paths is copied by the Dockerfile
- [x] every new path is classified in `config/data-classification.json` before
      the file exists, and the existing coverage test passes unchanged
- [ ] the local auth token written by the container is readable by the host CLI
      and the reverse, proven by a test rather than by inspection
- [x] the tracker database has exactly one writer under the chosen option, proven
      by a test that runs a scheduled-shape write and a container write against
      the same database and asserts both committed and neither corrupted
- [x] the API reports the built revision and build time, the UI displays it, and
      a test asserts a container built from an older tree is distinguishable from
      a current one
- [ ] `HARRIER_DEMO=1` inside the container writes to a temp directory and a test
      asserts nothing is written under `/app/data`
- [x] the image carries the external binary the configured provider seam
      resolves, at the path it advertises and at a pinned version, and the
      compose file supplies the credential that CLI authenticates with, proven
      by `services/api/tests/test_container.py::test_the_image_installs_the_cli_the_provider_seam_resolves`,
      `::test_the_cli_lands_where_the_image_says_it_did`,
      `::test_the_cli_version_is_pinned_rather_than_floating` and
      `::test_the_container_supplies_the_credential_the_cli_authenticates_with`
- [x] `just contract` produces no diff, because no route shape changes
- [x] the README documents the Docker Desktop start-on-login setting as machine
      configuration the repository cannot enforce, and does not claim otherwise
- [x] `just dev` continues to work unchanged for anyone who prefers it
- [ ] all gates green on PR

## Proof / origin

The failure that motivates this was observed directly and is reproducible from
the facts above: an orphaned uvicorn holding port 8000, reparented to init,
answering 200 on `/health` while `just dev` reported the bind failure and
carried on to start Vite against it.

The topology being changed is stated in `docs/adr/ADR-007-repo-layout-and-toolchain.md:87`.
The scheduler decision being preserved is `docs/adr/ADR-006-scheduling.md`. The
SPA mount is `services/api/src/harrier_api/app.py:721`. The trusted host list is
`services/api/src/harrier_api/localauth.py:54`. The token file and its mode are
`localauth.py:61`. Path resolution is `services/api/src/harrier/paths.py` and
`services/api/src/harrier/db.py:19`. The demo temp-directory rule is spec 021.
The exposure constraints are spec 035.

The old system's container topology (`job_server`, `gui`, cron scheduler) is
recorded in `docs/parity-matrix.md:134`, which already decided to drop the cron
container as a production path. This spec keeps that decision and revisits only
the daily-driver half.

## Out of scope

- **The scheduler.** launchd keeps the cadence. ADR-006 is not reopened.
- **Remote or multi-user access.** Loopback only, no auth redesign, spec 035
  stands as written.
- **Any route, contract, or UI change** beyond displaying the running revision.
- **CI images or a published image.** This image is built locally and never
  pushed anywhere.
- **Fixing the `just dev` trap.** It is a real defect, described in the Problem
  section because it is part of what happened, but repairing it is a separate
  change against a separate spec. This spec routes around it rather than
  touching it.
- **How `.env` reaches the process.** Nothing under `services/api/src/` loads a
  `.env` file, and the generated plists set no `EnvironmentVariables`. Whether
  scheduled runs and interactive runs actually receive `APIFY_TOKEN` today was
  not traced, and using `env_file` in compose would change the answer for the
  container only. That asymmetry deserves its own spec and is flagged, not
  absorbed here.
- **Linux or Windows support.** The daily driver is a Mac, per ADR-006.

## Migration

For the one operator:

1. Accept ADR-010, or reject it and this spec dies with it.
2. Stop any host uvicorn holding port 8000, including orphans.
   `lsof -nP -iTCP:8000 -sTCP:LISTEN` names the holder.
3. `docker compose up -d` from the repository root.
4. Enable Docker Desktop's start-on-login setting once, by hand.
5. launchd needs no change unless the implementation chooses the `docker exec`
   option, in which case `harrier schedule install` re-renders the plists and
   the spec is amended to record it.

`just dev` keeps working and remains the way to run with `--reload` while
editing.
