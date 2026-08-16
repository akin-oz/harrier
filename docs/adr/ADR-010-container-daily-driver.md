# ADR-010: The API and UI run in a container; launchd keeps the schedule

- Status: accepted
- Date: 2026-08-16
- Supersedes: the "not the daily driver" clause of ADR-007
- Preserves: ADR-006 in full

## Context

ADR-007 settled the toolchain and closed with a topology line: "Docker remains
only for the demo path (optional), not the daily driver; launchd plus local
processes are the production topology (ADR-006)." The container path it
sanctioned was never built. No Dockerfile or compose file exists in this
repository; the ones cited in `docs/parity-matrix.md:134` belong to the old
system at `~/job-hunt-local`.

"Local processes" turned out to mean one foreground `just dev` invocation with
no supervisor. That produced a silent failure: an earlier uvicorn had outlived
its shell, held port 8000, and answered health checks while serving an older
revision of the code. `just dev` reported the bind failure in one
line and continued, the Vite dev server proxied to the orphan, and the UI looked
correct. The recipe's own cleanup trap held the PID of the process that had just
failed to bind, so it cleaned up nothing.

The ADR-007 clause was not wrong about the demo. It was written before there was
evidence about what "local processes" costs daily.

## Options

### Keep launchd and local processes, fix the recipe

Pros: no new dependency, no ADR change, smallest diff. Cons: the recipe is not
the problem. A `just dev` with a correct trap still runs only while a terminal
is open, still has no restart across sleep or reboot, and still leaves the
operator to notice that the thing is down. It repairs one symptom of the
2026-08-16 failure and none of the causes.

### An in-app or launchd-managed supervisor for uvicorn on the host

Pros: no container, restart-on-failure via `KeepAlive`, consistent with ADR-006's
preference for OS-level supervision. Cons: gives the API a lifetime but not a
reproducible environment. The stale-code failure mode survives, because the
running process still reflects whatever the working tree was when it started.
It also puts a long-lived server under the same launchd that ADR-006
deliberately keeps independent of the API.

### The API and UI in a container, launchd untouched (recommended)

Pros: supervision, restart policy, and a reproducible environment in one
mechanism. The image is a fixed artifact, so "what is running" becomes a
question with an answer. The API already serves the built SPA
(`services/api/src/harrier_api/app.py:721`), so this is one container, not a
stack. Cons: introduces Docker as a daily dependency on the one machine that
matters, and creates a second process writing the tracker database across a
bind mount, which spec 051 has to resolve rather than assume.

### Everything in containers, including the schedule

Rejected without much deliberation, because ADR-006 already argued it. Moving
the cadence into a container rebuilds missed-run semantics, catch-up, and
sleep/wake handling that launchd provides, and makes the pipeline depend on the
container being up. The parity matrix reached the same conclusion from the old
system's cron container and dropped it.

## Decision

The API and the built SPA run as one container, published on `127.0.0.1:8000`,
with `restart: unless-stopped`.

launchd remains the scheduler of record, on the host, calling the host CLI.
ADR-006 stands in full and is not reopened. The container owns the interactive
surface; launchd owns the cadence. That split is the point of this decision: it
is the same separation ADR-006 made for the same reason, applied to the half
that had no supervisor.

Personal data does not enter the image. The database, config, and secrets stay
on the host and are bind-mounted, so ADR-008 is unaffected and the container
stays disposable.

The clause in ADR-007 is superseded only in its daily-driver half. The rest of
ADR-007, including the demo container path and the `just`-recipes-in-CI rule,
stands.

## Consequences

- Docker becomes a daily dependency on the daily driver. When Docker is not
  running, the UI is down. The schedule is not, which is the property worth
  keeping and the reason the scheduler did not move.
- Two processes now reach `data/harrier.db`: the container and the launchd host
  CLI. ADR-003's single write path is still the only path, but concurrency
  across a bind mount is a new question. Spec 051 requires the implementation to
  pick a resolution and prove it, rather than mounting the file and hoping.
- Stale code becomes a container-shaped risk instead of a process-shaped one. An
  image is stale until rebuilt, so spec 051 requires the running revision to be
  visible on the health path and in the UI. Without that this decision trades a
  failure that is invisible for a failure that is invisible and containerised.
- "Live all the time" depends on a Docker Desktop setting that lives on the
  machine, not in the repository. Documented as such, not claimed as enforced.
- macOS only, consistent with ADR-006.
