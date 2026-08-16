import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import type { components } from "@harrier/contract";

import { api } from "../../shared/api/client";
import "../../shared/ui/run.css";
import "./RunPanel.css";
import { TERMINAL_STATES, useRunStream } from "./useRunStream";
import type { EventSourceFactory, RunOut } from "./useRunStream";

type RunKind = components["schemas"]["StartRunIn"]["kind"];

export type { EventSourceFactory };

export function RunPanel({
  createEventSource = (url: string) => new EventSource(url),
}: {
  createEventSource?: EventSourceFactory;
}) {
  const queryClient = useQueryClient();
  // Collapsed by default. A healthy run is thousands of lines nobody needs
  // to read, and it used to push the tracker off the screen entirely. It
  // opens itself on failure, the one case where the log is the point.
  const [expanded, setExpanded] = useState(false);
  const logRef = useRef<HTMLPreElement | null>(null);

  const stream = useRunStream(createEventSource);
  const { run, lines, progress, disconnected, failed } = stream;

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

  const startMutation = useMutation({
    mutationFn: async (kind: RunKind) => {
      const { data, error } = await api.POST("/runs", { body: { kind } });
      if (error !== undefined) {
        throw new Error(`start failed: ${JSON.stringify(error)}`);
      }
      // The contract now declares 403, so a refusal is a shape the caller has
      // to handle rather than one it can assume away (spec 035). A missing
      // body here means the API refused without an error payload.
      if (data === undefined) {
        throw new Error("start refused: the local API token was not accepted");
      }
      return data;
    },
    onSuccess: (data) => {
      setExpanded(false);
      stream.begin(data);
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
      if (data === undefined) {
        throw new Error("cancel refused: the local API token was not accepted");
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
  // A started run with nothing streamed yet is waiting, not idle and not
  // broken. Without this the panel showed a run id and then nothing.
  const waiting = run !== null && lines.length === 0 && !TERMINAL_STATES.has(run.state);

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
      {disconnected && (
        <p role="status" className="run-panel__disconnected">
          Lost the log stream. The run may still be going; its state above is refreshed from the
          server.
        </p>
      )}
      {waiting && <p className="run-panel__last-line">waiting for the first line…</p>}
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
