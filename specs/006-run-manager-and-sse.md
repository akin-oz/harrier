---
spec: 006
title: Run manager, SSE channel, live run streaming
status: shipped
approved: yes
milestone: M1
depends: [005]
---

# Spec 006: Run manager, SSE channel, live run streaming

Refined from the stub before implementation; scope below is the real scope.

## Amendment (stated, not silent)

The stub's acceptance said "a dry-run import streams log lines". Importers do
not exist until M2 (specs 008 to 011), so this spec proves the machinery with
a purpose-built `demo` run kind: a real CLI subprocess that emits the
structured progress protocol over several seconds. The discovery run kind
plugs into the same machinery in spec 011; nothing here is throwaway except
the demo command's registration as a user-visible kind.

## Scope

### Run manager (services/api/src/harrier_api/runs.py)

- In-process registry of runs: id, kind, state (queued, running, succeeded,
  failed, cancelled), timestamps, command, exit code, ordered event list.
- Execution: `asyncio.create_subprocess_exec` of a `harrier` CLI entry point;
  stdout captured line by line. Cancellation: SIGTERM, SIGKILL after a 5 s
  grace period (ADR-004).
- Single-active-run policy: starting a kind while one is active returns the
  active run instead of erroring (ADR-004 concurrency policy).
- On-disk journal: one JSON line per state change to `data/runs/journal.jsonl`
  (never-in-git) so a restarted server can list recent runs.

### Progress line protocol

- A subprocess stdout line starting with `::harrier::` followed by JSON is a
  structured event (`{"event": "progress", "step": n, "total": m, "message"}`);
  every other line is a `log_line` event. The CLI renders human text; the run
  manager parses. Pinned by tests on both sides.

### API

- `POST /runs` (body: kind), `GET /runs`, `GET /runs/{id}`,
  `POST /runs/{id}/cancel`: JSON, in the OpenAPI contract.
- `GET /runs/{id}/events`: SSE (`text/event-stream`), events carry
  monotonically increasing ids; `Last-Event-ID` replays missed events from
  the run's event list. SSE payload shapes are outside the OpenAPI document
  (a contract-seam limitation noted in docs/aie-feedback.md is not needed;
  this is an OpenAPI limitation, recorded here instead): the event JSON is
  pinned by API tests and mirrored by a hand-written type in the web feature,
  with a comment naming this spec.

### CLI

- `harrier demo-run --steps N --delay S`: emits progress protocol lines and
  human lines, exits 0; on SIGTERM prints a final line and exits nonzero.

### Web (apps/web)

- `features/runs`: a RunPanel with start and cancel buttons (REST via the
  generated client) and a live log view fed by an `EventSource` (injectable
  factory so tests stub it). Reconnect uses Last-Event-ID replay natively.
- TrackerPage hosts the panel above the table.

## Acceptance criteria

- [ ] A started demo run streams progress and log events to the browser live
- [ ] Cancel terminates the subprocess; the run ends state=cancelled
- [ ] Reconnect mid-run replays missed events (tested via Last-Event-ID)
- [ ] Second start during an active run returns the active run id
- [ ] Journal lines written per state change; GET /runs lists a finished run
      after server restart (journal-backed)
- [ ] All gates green on PR

## Proof / origin

docs/adr/ADR-004-long-running-work.md; spec 005 (contract seam this extends).

## Out of scope

Real discovery runs (spec 011 registers the kind), a durable queue, run
history UI beyond the list endpoint, parallel runs of the same kind, and
artifact-render run kinds (specs 013 to 015).
