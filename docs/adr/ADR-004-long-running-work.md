# ADR-004: Long-running work

- Status: accepted
- Date: 2026-08-08

## Context

Three classes of long work exist today, all synchronous scripts: discovery runs (seconds
to minutes; the Apify path polls a remote actor with timeouts up to 1800 s, see
`scripts/import_apify_linkedin_jobs.py`), Playwright PDF renders (seconds, but with a
Chromium boot), and batch evaluations (`scripts/evaluate_prospects.py`, one LLM call per
prospect). The GUI today blocks on subprocess completion and shows nothing until the end.
The new GUI needs: start a run, watch progress and logs live, cancel it, and see the
outcome reflected in the tracker.

Scope of this ADR: execution model, progress transport, cancellation, log streaming.

## Execution model

### Options

1. **FastAPI BackgroundTasks**: fire-and-forget in the server event loop or threadpool.
   No handle for cancellation, no progress identity, dies silently with the request
   context. Fine for "touch the export after a write", wrong for runs.
2. **External task runner (Celery, RQ, arq)**: real queues, retries, workers. All
   require a broker (Redis), which violates local-first and no-new-services for a
   single-user tool. Rejected.
3. **In-process run manager executing subprocesses (recommended)**: a small module in
   the API service that owns a registry of runs. Each run executes the same entry point
   the CLI uses, as a child process (`asyncio.create_subprocess_exec`), with stdout and
   stderr captured line by line into a ring buffer and an event stream. Runs have IDs,
   states (queued, running, succeeded, failed, cancelled), start/end times, and a
   structured result file, mirroring the current `incoming/job_imports_run.json`
   behavior.

Why subprocesses and not in-process coroutines: the pipeline is CPU-and-IO mixed Python
plus Playwright; a child process gives clean cancellation (terminate, then kill after a
grace period), crash isolation from the API server, guaranteed log capture, and it
keeps the CLI and the API running literally the same code path, which is what keeps
CLI/API/scheduler behavior identical. Scheduled launchd runs invoke the same CLI
directly, so a dead API server never blocks discovery (ADR-006).

Concurrency policy: one discovery run at a time (a second start request returns the
running run's ID); artifact renders are per-slug locked; a global cap keeps Playwright
instances bounded.

## Progress transport

### Options

1. **WebSocket**: bidirectional, but the browser-to-server direction has no use here
   (start and cancel are natural REST POSTs). Costs: connection lifecycle management,
   no native reconnect, proxy quirks.
2. **SSE (recommended)**: one-directional server-to-client events over plain HTTP.
   Native `EventSource` auto-reconnect with `Last-Event-ID` replay from the ring
   buffer, trivial to consume from the SPA, trivial to debug with curl. Fits the actual
   shape: the client sends commands rarely (REST) and receives events constantly.

## Decision

In-process run manager with subprocess execution. REST for commands:
`POST /runs` (start), `POST /runs/{id}/cancel`, `GET /runs`, `GET /runs/{id}`.
SSE for events: `GET /runs/{id}/events` streams typed events
(`log_line`, `progress`, `state_change`, `result`) with monotonically increasing event
IDs so reconnects resume without loss. The importers gain a structured progress line
protocol on stdout (JSON lines prefixed, human text passes through as `log_line`), which
the CLI renders as text and the run manager parses into `progress` events. Cancellation
is cooperative first (SIGTERM, importers flush state and summaries) with SIGKILL after
a grace period; a cancelled run's partial seen-state writes are safe because seen-state
updates are per-item and idempotent.

## Consequences

- The GUI gets live logs and progress for every run class through one mechanism.
- The run registry is in-memory with a small on-disk journal so a server restart lists
  recent runs; it is not a durable queue, and the README says so. Scheduled work does
  not depend on it (launchd calls the CLI).
- The structured-progress line protocol becomes part of the importer contract and is
  covered by tests.
- If multi-machine or durable queuing ever becomes real, that is a new ADR; nothing
  here leaks into the domain layer.
