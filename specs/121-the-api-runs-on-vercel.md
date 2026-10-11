---
spec: 121
title: The hosted API runs on Vercel beside the SPA, and runs move to a durable executor
status: proposed
approved: no
milestone: M10
depends: [102, 104, 105, 106, 108, 110, 112]
---

# Spec 121: The hosted API runs on Vercel beside the SPA, and runs move to a durable executor

## Problem

ADR-013 decision 7 puts the hosted API on Fly and has Vercel serve only the
SPA build (`docs/adr/ADR-013-hosted-multi-tenant-deployment.md`, decision
7). Specs 106, 108 and 110 build on that: one Fly machine with auto-stop
off for runs (106), a scheduler in a Fly process group (108), and two
Vercel projects that hold the SPA only (110).

On 2026-10-11 Akin chose one Vercel project with services instead: the SPA
and the API deploy together, on one domain, from `vercel.json`. That
changes an accepted ADR and three approved specs, so this spec states what
the move does, and what it cannot do as the code stands.

The request path moves cleanly. The SPA already calls the API at the
relative path `/api` (`apps/web/src/shared/api/client.ts:10`), and the API
already strips that prefix itself (`services/api/src/harrier_api/app.py:1120`).
Nothing in harrier calls one service from another, so no service binding
is needed. On one origin, spec 104's cross-origin list and spec 114's
configured API URL also become unnecessary.

Runs do not move as they are. The run manager starts each run as a child
process that outlives the request that started it, keeps the run registry
in the API process's memory, and streams events from that memory (ADR-004,
`services/api/src/harrier_api/runs.py`). Vercel's documented limits, read
2026-10-11, break each of those:

- A function invocation, including a streamed response, ends at its
  maximum duration: 300 s on Hobby, 800 s on Pro and Enterprise, with a
  1800 s maximum in beta for Node.js, Bun and Python functions only
  (https://vercel.com/docs/functions/limitations).
- A container function scales to many instances and is stopped after 5
  minutes without traffic in production, with a 30 s grace period after
  `SIGTERM` (https://vercel.com/docs/functions/runtimes/container). A
  registry in one instance's memory is invisible to the next instance and
  is lost at scale-down, with any child process still running.
- Request and response bodies are limited to 4.5 MB
  (https://vercel.com/docs/functions/limitations).
- The container runtime and services are both in beta
  (https://vercel.com/docs/functions/runtimes/container,
  https://vercel.com/docs/services).

## Scope

This spec decides the topology and the run executor, and splits the build.
It changes no code itself.

1. ADR-013 decision 7 is amended to the text under Behavior, with an
   "Amended (spec 121)" note.
2. The run executor is decided (Open decision 1) and its build becomes its
   own spec, the next free number at approval.
3. The `vercel.json` and container files the deploy needs are named and
   classified here; spec 110 (amended) creates them.
4. The changes this makes to approved specs are listed under "Changes to
   approved specs" for Akin to approve.

## Behavior

**Topology after this spec.** One Vercel project per environment
(staging, production), each with two services:

```json
{
  "$schema": "https://openapi.vercel.sh/vercel.json",
  "services": {
    "web": {
      "root": "apps/web",
      "framework": "vite"
    },
    "api": {
      "root": "services/api",
      "runtime": "container",
      "entrypoint": "Dockerfile.vercel"
    }
  },
  "rewrites": [
    { "source": "/api/(.*)", "destination": { "service": "api" } },
    { "source": "/(.*)", "destination": { "service": "web" } }
  ]
}
```

- `web` is public at `/`; `api` is public at `/api/`. Neither is internal,
  because the browser is the only caller of the API.
- No `bindings`: no service calls another.
- The `api` image is built from `services/api/Dockerfile.vercel`, a hosted
  image with Chromium and poppler and without the local daily driver's
  bind mounts, launchd hand-off or CLI LLM providers. The root `Dockerfile`
  stays the local container (ADR-010) and is not deployed.
- The API listens on `$PORT` (default 80 for a container function).
- The region is Frankfurt (`fra1`) for both services, matching spec 102
  decision 2 and Supabase `eu-central-1`.

**ADR-013 decision 7, amended text:**

> 7. **Topology.** One Vercel project per environment serves the SPA and
>    the API as two services on one origin: the SPA at `/`, the API at
>    `/api/` as a container function. Supabase runs the database, auth and
>    storage in the same jurisdiction. Runs do not execute inside API
>    requests: the run executor of spec 121 runs them.
>
>    Amended (spec 121). This said Fly runs the API image and Vercel serves
>    the SPA only. Akin chose one Vercel project on 2026-10-11. The API's
>    request path fits a function; its run manager does not, because a
>    function ends at its maximum duration and an instance holds no state
>    another instance can see.

**Runs.** Every run starts by writing its record to the database and
handing it to the executor; the request returns at once. Run events are
written to the database as the run produces them, and the event stream
(`GET /runs/{id}/events`) reads them from there, so any instance can serve
any stream. A stream ends at the function's maximum duration and the
browser reconnects with `Last-Event-ID` (spec 104 decision 4, spec 114),
which already resumes without loss.

## Failure modes

- **A run longer than the executor allows.** Refused before it starts if
  its kind is known to exceed the limit; otherwise recorded as
  `timed_out` with the events it wrote, never left as running.
- **An instance stops mid-run** (scale-down, deploy). The run's record
  says so on the next read (`interrupted`), and the run is not silently
  repeated.
- **A response over 4.5 MB** (a large export or PDF). Served from Storage
  (spec 107) by URL, never as a function body.
- **The container runtime leaves beta with different limits.** Spec 110's
  staging checks (spec 115) record the limits actually observed.

## Acceptance criteria

- [ ] ADR-013 decision 7 carries the amended text and its note.
- [ ] The executor spec (Vercel Queues, Open decision 1) exists with
      `approved: no`.
- [ ] Every approved spec named under "Changes to approved specs" carries
      a dated amendment, approved by Akin, before any of this is built.
- [ ] `vercel.json`, `services/api/Dockerfile.vercel` and `.vercel/**` are
      classified in `config/data-classification.json` before they exist
      (`.vercel/**` never in git; the others public), and
      `services/api/tests/test_classification_coverage.py` passes.

## Changes to approved specs

- **106:** drops "one Fly machine with auto-stop off" and the run login
  role's Fly secret; a run executes in the executor this spec chooses,
  bound to its owner as 106 already requires.
- **108:** the scheduler is not a Fly process group; it becomes a Vercel
  Cron job, or the executor's own schedule, that only enqueues due owners'
  runs, under the narrow schedule-reader role 108 already defines.
- **110:** two Vercel projects become one project per environment with two
  services; no Fly app; the API image is `services/api/Dockerfile.vercel`;
  secrets live in Vercel project environments instead of Fly secrets;
  `vercel.json` sits at the repository root instead of `apps/web`.
- **104:** the cross-origin list and the hosted-host list collapse to the
  one Vercel origin per environment.
- **114:** `VITE_HARRIER_API_URL` is no longer needed; the SPA keeps calling
  `/api` on its own origin.

## Honest limitations

- The limits above are Vercel's documented ones as read on 2026-10-11, for
  products in beta. None has been measured against harrier.
- Whether harrier's longest runs (discovery over paid sources, tailoring
  with an LLM and a PDF render) fit 800 s has not been measured. Spec 029's
  run records hold durations for the local product; they are the first
  evidence to read.
- The container image size limit for container functions is not stated on
  the pages read; the general function limit is 250 MB, or up to 5 GB for
  large functions (beta) on Node.js, Bun and Python. An image with
  Chromium may need checking against it.

## Migration

None for the local product: the root `Dockerfile`, ADR-010's container and
the in-process run manager stay as they are locally.

## Options weighed

- **The suggested three-service layout** (`app` as the root `Dockerfile`
  plus `web` and `api`). Rejected: `app` already contains both the API and
  the built SPA, so with its catch-all `web` never receives traffic, and
  the root `Dockerfile` is the local daily driver with bind-mounted
  personal data.
- **Keep ADR-013 as written** (SPA on Vercel, API on Fly). Akin chose
  against it on 2026-10-11.

## Open decisions for Akin

Akin took the recommended answer to each on 2026-10-11, and confirmed the
`vercel.json` above: services `web` and `api`, both public (`web` at `/`,
`api` at `/api/`), no bindings.

1. **Where runs execute.**
   - Taken 2026-10-11: (a).
   - (a) **Vercel Queues with a push consumer** (recommended to try first):
     the API enqueues; a consumer function runs one run per message within
     the maximum duration. All on Vercel. Requires every run kind to fit
     800 s (Pro) and an event log in the database. Python SDK exists
     (https://vercel.com/docs/queues/python-sdk).
   - (b) **Vercel Workflows**: no duration limit by design, but Python
     support was not confirmed on the pages read.
   - (c) **A small worker kept on Fly for runs only**: the run manager
     unchanged, but a second vendor stays, which this move set out to
     avoid.
2. **Plan.** Taken 2026-10-11: Pro. Pro (800 s, 4 GB memory) is needed for runs of any length;
   Hobby stops every invocation at 300 s.
3. Taken 2026-10-11: **one project per environment** (recommended, matches spec 102 decision
   4) or one project with a custom staging environment.

## Proof / origin

- `docs/adr/ADR-013-hosted-multi-tenant-deployment.md`, decision 7.
- `docs/adr/ADR-004-long-running-work.md` and
  `services/api/src/harrier_api/runs.py`: the run manager.
- `apps/web/src/shared/api/client.ts:10` and
  `services/api/src/harrier_api/app.py:1120`: the `/api` prefix.
- Vercel docs, read 2026-10-11: https://vercel.com/docs/services,
  https://vercel.com/docs/services/pricing,
  https://vercel.com/docs/functions/runtimes/container,
  https://vercel.com/docs/functions/limitations,
  https://vercel.com/docs/queues/python-sdk.

## Out of scope

- Writing `vercel.json`, `Dockerfile.vercel` or any deploy configuration:
  spec 110 as amended.
- The run executor's implementation: its own spec.
- The local product.
