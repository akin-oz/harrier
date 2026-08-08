import { useCallback, useEffect, useRef, useState } from "react";

import type { components } from "@harrier/contract";

import { api } from "../../shared/api/client";

type RunOut = components["schemas"]["RunOut"];

// SSE payload shape: outside the OpenAPI document, pinned by spec 006 and
// mirrored by services/api tests (test_format_sse_shape).
interface RunEventPayload {
  type: string;
  line?: string;
  step?: number;
  total?: number;
  message?: string;
  state?: string;
}

export type EventSourceFactory = (url: string) => EventSource;

const TERMINAL_STATES = new Set(["succeeded", "failed", "cancelled"]);

function describeEvent(payload: RunEventPayload): string {
  if (payload.type === "log_line") {
    return payload.line ?? "";
  }
  if (payload.type === "progress") {
    return `progress ${String(payload.step ?? "?")}/${String(payload.total ?? "?")}: ${payload.message ?? ""}`;
  }
  if (payload.type === "state_change") {
    return `run is ${payload.state ?? "unknown"}`;
  }
  return JSON.stringify(payload);
}

export function RunPanel({
  createEventSource = (url: string) => new EventSource(url),
}: {
  createEventSource?: EventSourceFactory;
}) {
  const [run, setRun] = useState<RunOut | null>(null);
  const [lines, setLines] = useState<readonly string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const sourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    return () => {
      sourceRef.current?.close();
    };
  }, []);

  const subscribe = useCallback(
    (runId: string) => {
      sourceRef.current?.close();
      const source = createEventSource(`/api/runs/${runId}/events`);
      source.onmessage = (message: MessageEvent<string>) => {
        const payload = JSON.parse(message.data) as RunEventPayload;
        setLines((existing) => [...existing, describeEvent(payload)]);
        if (payload.type === "state_change") {
          const state = payload.state ?? "";
          setRun((existing) =>
            existing ? { ...existing, state: state as RunOut["state"] } : existing,
          );
          if (TERMINAL_STATES.has(state)) {
            source.close();
          }
        }
      };
      sourceRef.current = source;
    },
    [createEventSource],
  );

  const start = useCallback(async () => {
    setError(null);
    setLines([]);
    const { data, error: apiError } = await api.POST("/runs", { body: { kind: "demo" } });
    if (apiError !== undefined) {
      setError(`start failed: ${JSON.stringify(apiError)}`);
      return;
    }
    setRun(data);
    subscribe(data.id);
  }, [subscribe]);

  const cancel = useCallback(async () => {
    if (run === null) {
      return;
    }
    const { error: apiError } = await api.POST("/runs/{run_id}/cancel", {
      params: { path: { run_id: run.id } },
    });
    if (apiError !== undefined) {
      setError(`cancel failed: ${JSON.stringify(apiError)}`);
    }
  }, [run]);

  const active = run !== null && !TERMINAL_STATES.has(run.state);

  return (
    <section aria-label="runs">
      <h3>Runs</h3>
      <p>
        <button
          type="button"
          onClick={() => {
            void start();
          }}
          disabled={active}
        >
          Start demo run
        </button>{" "}
        <button
          type="button"
          onClick={() => {
            void cancel();
          }}
          disabled={!active}
        >
          Cancel
        </button>{" "}
        {run !== null && (
          <span>
            run {run.id}: <strong>{run.state}</strong>
          </span>
        )}
      </p>
      {error !== null && <p role="alert">{error}</p>}
      {lines.length > 0 && <pre aria-label="run log">{lines.join("\n")}</pre>}
    </section>
  );
}
