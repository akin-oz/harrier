import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";

import type { components } from "@harrier/contract";

import { api } from "../../shared/api/client";

export type RunOut = components["schemas"]["RunOut"];
type RunEventPayload = components["schemas"]["RunEventOut"];

export type EventSourceFactory = (url: string) => EventSource;

export const TERMINAL_STATES = new Set<RunOut["state"]>(["succeeded", "failed", "cancelled"]);

export interface ProgressState {
  step: number | null;
  total: number | null;
}

export function describeEvent(payload: RunEventPayload): string {
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

/**
 * Watching one run: its state, its log lines, its progress.
 *
 * Extracted from RunPanel when the Apply page needed the same thing for a
 * per-job run (spec 047). A second copy of the SSE handling is exactly the
 * "second implementation" that spec names as a failure mode, and the two
 * would have drifted on reconnect handling first.
 */
export function useRunStream(createEventSource: EventSourceFactory) {
  const queryClient = useQueryClient();
  const [runId, setRunId] = useState<string | null>(null);
  const [lines, setLines] = useState<readonly string[]>([]);
  // The last line the process actually printed, which is not the same as the
  // last line in `lines`: a state_change appends "run is failed" after it, so
  // a caller showing the tail of the log as the reason for a failure would
  // show the failure rather than its cause (spec 047).
  const [lastLogLine, setLastLogLine] = useState<string | null>(null);
  const [progress, setProgress] = useState<ProgressState | null>(null);
  const [disconnected, setDisconnected] = useState(false);
  const sourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    return () => {
      sourceRef.current?.close();
    };
  }, []);

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
        if (payload.type === "log_line" && (payload.line ?? "") !== "") {
          setLastLogLine(payload.line ?? "");
        }
        if (payload.type === "progress") {
          setProgress({ step: payload.step ?? null, total: payload.total ?? null });
        }
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
        // The stream dropping is not the run failing: say so, and refetch
        // server truth rather than leaving the panel looking stuck.
        setDisconnected(true);
        void queryClient.invalidateQueries({ queryKey: ["run", id] });
      };
      sourceRef.current = source;
    },
    [createEventSource, queryClient],
  );

  /** Adopt a freshly started run and begin streaming it. */
  const begin = useCallback(
    (started: RunOut) => {
      setLines([]);
      setLastLogLine(null);
      setProgress(null);
      setDisconnected(false);
      setRunId(started.id);
      queryClient.setQueryData<RunOut>(["run", started.id], started);
      subscribe(started.id);
    },
    [queryClient, subscribe],
  );

  const active = run !== null && !TERMINAL_STATES.has(run.state);
  const failed = run?.state === "failed";

  return { run, runId, lines, lastLogLine, progress, disconnected, begin, active, failed };
}
