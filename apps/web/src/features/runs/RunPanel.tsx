import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";

import type { components } from "@harrier/contract";

import { api } from "../../shared/api/client";

type RunOut = components["schemas"]["RunOut"];
// SSE message payload: generated from the contract (spec 006 review follow-up);
// the stream route declares RunEventOut on its response documentation.
type RunEventPayload = components["schemas"]["RunEventOut"];

export type EventSourceFactory = (url: string) => EventSource;

const TERMINAL_STATES = new Set<RunOut["state"]>(["succeeded", "failed", "cancelled"]);

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

async function fetchRun(runId: string): Promise<RunOut> {
  const { data, error } = await api.GET("/runs/{run_id}", {
    params: { path: { run_id: runId } },
  });
  if (error !== undefined) {
    throw new Error(`getRun failed: ${JSON.stringify(error)}`);
  }
  return data;
}

export function RunPanel({
  createEventSource = (url: string) => new EventSource(url),
}: {
  createEventSource?: EventSourceFactory;
}) {
  const queryClient = useQueryClient();
  const [runId, setRunId] = useState<string | null>(null);
  const [lines, setLines] = useState<readonly string[]>([]);
  const sourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    return () => {
      sourceRef.current?.close();
    };
  }, []);

  // TanStack Query owns the run resource (ADR-001); SSE feeds the cache, and
  // a broken stream invalidates it so server truth wins over a stuck state.
  const runQuery = useQuery({
    queryKey: ["run", runId],
    queryFn: () => fetchRun(runId ?? ""),
    enabled: runId !== null,
  });
  const run = runQuery.data ?? null;

  const subscribe = useCallback(
    (id: string) => {
      sourceRef.current?.close();
      const source = createEventSource(`/api/runs/${id}/events`);
      source.onmessage = (message: MessageEvent<string>) => {
        const payload = JSON.parse(message.data) as RunEventPayload;
        setLines((existing) => [...existing, describeEvent(payload)]);
        if (payload.type === "state_change" && payload.state != null) {
          const state = payload.state;
          queryClient.setQueryData<RunOut>(["run", id], (existing) =>
            existing ? { ...existing, state, exit_code: payload.exit_code ?? null } : existing,
          );
          if (TERMINAL_STATES.has(state)) {
            source.close();
          }
        }
      };
      source.onerror = () => {
        void queryClient.invalidateQueries({ queryKey: ["run", id] });
      };
      sourceRef.current = source;
    },
    [createEventSource, queryClient],
  );

  const startMutation = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/runs", { body: { kind: "demo" } });
      if (error !== undefined) {
        throw new Error(`start failed: ${JSON.stringify(error)}`);
      }
      return data;
    },
    onSuccess: (data) => {
      setLines([]);
      setRunId(data.id);
      queryClient.setQueryData<RunOut>(["run", data.id], data);
      subscribe(data.id);
    },
  });

  const cancelMutation = useMutation({
    mutationFn: async (id: string) => {
      const { data, error } = await api.POST("/runs/{run_id}/cancel", {
        params: { path: { run_id: id } },
      });
      if (error !== undefined) {
        throw new Error(`cancel failed: ${JSON.stringify(error)}`);
      }
      return data;
    },
    onSuccess: (data) => {
      queryClient.setQueryData<RunOut>(["run", data.id], data);
    },
  });

  const active = run !== null && !TERMINAL_STATES.has(run.state);
  const mutationError = startMutation.error ?? cancelMutation.error;

  return (
    <section aria-label="runs">
      <h3>Runs</h3>
      <p>
        <button
          type="button"
          onClick={() => {
            startMutation.mutate();
          }}
          disabled={active || startMutation.isPending}
        >
          Start demo run
        </button>{" "}
        <button
          type="button"
          onClick={() => {
            if (run !== null) {
              cancelMutation.mutate(run.id);
            }
          }}
          disabled={!active || cancelMutation.isPending}
        >
          Cancel
        </button>{" "}
        {run !== null && (
          <span>
            run {run.id}: <strong>{run.state}</strong>
          </span>
        )}
      </p>
      {mutationError !== null && <p role="alert">{mutationError.message}</p>}
      {lines.length > 0 && <pre aria-label="run log">{lines.join("\n")}</pre>}
    </section>
  );
}
