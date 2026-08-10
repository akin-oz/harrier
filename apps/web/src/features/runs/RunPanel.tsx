import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";

import type { components } from "@harrier/contract";

import { api } from "../../shared/api/client";
import "./RunPanel.css";

type RunOut = components["schemas"]["RunOut"];
type RunKind = components["schemas"]["StartRunIn"]["kind"];
type RunEventPayload = components["schemas"]["RunEventOut"];

export type EventSourceFactory = (url: string) => EventSource;

const TERMINAL_STATES = new Set<RunOut["state"]>(["succeeded", "failed", "cancelled"]);

interface ProgressState {
  step: number | null;
  total: number | null;
}

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
  const [progress, setProgress] = useState<ProgressState | null>(null);
  // Collapsed by default. A healthy run is thousands of lines nobody needs
  // to read, and it used to push the tracker off the screen entirely. It
  // opens itself on failure, the one case where the log is the point.
  const [expanded, setExpanded] = useState(false);
  const sourceRef = useRef<EventSource | null>(null);
  const logRef = useRef<HTMLPreElement | null>(null);

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
  const failed = run?.state === "failed";

  useEffect(() => {
    if (failed) {
      setExpanded(true);
    }
  }, [failed]);

  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [lines, expanded]);

  const subscribe = useCallback(
    (id: string) => {
      sourceRef.current?.close();
      const source = createEventSource(`/api/runs/${id}/events`);
      source.onmessage = (message: MessageEvent<string>) => {
        const payload = JSON.parse(message.data) as RunEventPayload;
        setLines((existing) => [...existing, describeEvent(payload)]);
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
        void queryClient.invalidateQueries({ queryKey: ["run", id] });
      };
      sourceRef.current = source;
    },
    [createEventSource, queryClient],
  );

  const startMutation = useMutation({
    mutationFn: async (kind: RunKind) => {
      const { data, error } = await api.POST("/runs", { body: { kind } });
      if (error !== undefined) {
        throw new Error(`start failed: ${JSON.stringify(error)}`);
      }
      return data;
    },
    onSuccess: (data) => {
      setLines([]);
      setProgress(null);
      setExpanded(false);
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
  // Narrowed once into a plain pair, so the JSX does not re-test what this
  // already established.
  const progressBar =
    run?.state === "running" && progress !== null && progress.total !== null && progress.total > 0
      ? { step: progress.step ?? 0, total: progress.total }
      : null;
  const lastLine = lines.length > 0 ? lines[lines.length - 1] : null;

  return (
    <section aria-label="runs" className="run-panel">
      <div className="run-panel__row">
        <h3 className="run-panel__title">Runs</h3>
        <div className="run-panel__status">
          {run !== null ? (
            <>
              <span className={`run-dot run-dot--${run.state}`} aria-hidden="true" />
              <span>
                run {run.id}: <strong>{run.state}</strong>
              </span>
            </>
          ) : (
            <>
              <span className="run-dot run-dot--idle" aria-hidden="true" />
              <span className="run-panel__idle">idle</span>
            </>
          )}
        </div>
      </div>
      {progressBar !== null && (
        <div
          className="run-progress"
          role="progressbar"
          aria-valuenow={progressBar.step}
          aria-valuemin={0}
          aria-valuemax={progressBar.total}
        >
          <span
            className="run-progress__fill"
            style={{ width: `${String((progressBar.step / progressBar.total) * 100)}%` }}
          />
        </div>
      )}
      <div className="run-panel__actions">
        <button
          type="button"
          onClick={() => {
            startMutation.mutate("discovery");
          }}
          disabled={active || startMutation.isPending}
        >
          Run discovery
        </button>
        <button
          type="button"
          onClick={() => {
            startMutation.mutate("demo");
          }}
          disabled={active || startMutation.isPending}
        >
          Start demo run
        </button>
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
        </button>
        {lines.length > 0 && (
          <button
            type="button"
            className="run-panel__toggle"
            aria-expanded={expanded}
            aria-controls="run-log"
            onClick={() => {
              setExpanded((value) => !value);
            }}
          >
            {expanded ? "Hide log" : "Show log"}
          </button>
        )}
      </div>
      {mutationError !== null && (
        <p role="alert" className="run-panel__error">
          {mutationError.message}
        </p>
      )}
      {lastLine !== null && !expanded && (
        <p className={`run-panel__last-line${failed ? " run-panel__last-line--failed" : ""}`}>
          {lastLine}
        </p>
      )}
      {lines.length > 0 && expanded && (
        <pre
          id="run-log"
          aria-label="run log"
          ref={logRef}
          className={`run-log${failed ? " run-log--failed" : ""}`}
        >
          {lines.join("\n")}
        </pre>
      )}
    </section>
  );
}
