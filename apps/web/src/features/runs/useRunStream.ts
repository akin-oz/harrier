import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";

import type { components } from "@harrier/contract";

import { api } from "../../shared/api/client";
import { StreamRefused, readRunEvents } from "../../shared/api/stream";

export type RunOut = components["schemas"]["RunOut"];
type RunEventPayload = components["schemas"]["RunEventOut"];

// How long to wait before opening the stream again after it failed, or after
// the server keeps ending it at once. A chosen value, not a measured one
// (spec 114, Honest limitations).
export const RETRY_DELAY_MS = 3000;

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
 * Resolves after `ms`, or at once when `signal` aborts. A pause that ends on
 * its timer takes its abort listener with it, so a long run that reconnects
 * many times does not leave one on the signal per attempt.
 */
export function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    const onAbort = (): void => {
      clearTimeout(timer);
      resolve();
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

/**
 * Watching one run: its state, its log lines, its progress.
 *
 * Extracted from RunPanel when the Apply page needed the same thing for a
 * per-job run (spec 047). A second copy of the stream handling is exactly the
 * "second implementation" that spec names as a failure mode, and the two
 * would have drifted on reconnect handling first.
 *
 * The stream is read with `fetch` through the generated client (spec 114), so
 * the hosted API's bearer token reaches it. Unlike the browser's own
 * event-source reader it does not reconnect itself, so this hook does: with
 * `Last-Event-ID` set to the last event it received, when the server ends the
 * stream before the run is terminal (hosted, the token expired), and after a
 * failure.
 */
export function useRunStream() {
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
  // The open connection and everything that follows it. Aborting it is how a
  // new run, an unmount and a terminal state each end the old one.
  const openRef = useRef<AbortController | null>(null);

  useEffect(() => {
    return () => {
      openRef.current?.abort();
    };
  }, []);

  const runQuery = useQuery({
    queryKey: ["run", runId],
    queryFn: () => fetchRun(runId ?? ""),
    enabled: runId !== null,
  });
  const run = runQuery.data ?? null;

  const follow = useCallback(
    async (id: string, controller: AbortController) => {
      const signal = controller.signal;
      // A function, so each read is of the signal as it is now: the checks
      // below come after awaits, during which it may have aborted.
      const stopped = (): boolean => signal.aborted;
      const terminal = (): boolean => {
        const known = queryClient.getQueryData<RunOut>(["run", id]);
        return known !== undefined && TERMINAL_STATES.has(known.state);
      };
      let lastEventId = 0;
      // Attempts in a row that ended cleanly having delivered nothing.
      let emptyEnds = 0;
      while (!stopped()) {
        let delivered = false;
        try {
          for await (const message of readRunEvents(id, { lastEventId, signal })) {
            const eventId = Number(message.lastEventId);
            // The server replays from the id it was given, so an event at or
            // before the last one is not shown again.
            if (Number.isFinite(eventId) && eventId > 0 && eventId <= lastEventId) continue;
            let payload: RunEventPayload;
            try {
              payload = JSON.parse(message.data) as RunEventPayload;
            } catch {
              continue;
            }
            if (stopped()) return;
            delivered = true;
            if (Number.isFinite(eventId) && eventId > lastEventId) lastEventId = eventId;
            // A reconnect that delivers an event is a stream that recovered.
            setDisconnected(false);
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
                controller.abort();
                return;
              }
            }
          }
          // The server ended the stream before the run did.
          emptyEnds = delivered ? 0 : emptyEnds + 1;
          if (terminal()) return;
          if (emptyEnds >= 2) await pause(RETRY_DELAY_MS, signal);
        } catch (error) {
          if (stopped()) return;
          // The run is gone, or another owner's (spec 104 answers 404 for
          // both): nothing to follow. A 401 is the middleware's, which has
          // ended the session and unmounts this with the shell.
          if (error instanceof StreamRefused && (error.status === 404 || error.status === 401)) {
            void queryClient.invalidateQueries({ queryKey: ["run", id] });
            return;
          }
          // The stream dropping is not the run failing: say so, and refetch
          // server truth rather than leaving the panel looking stuck.
          emptyEnds = 0;
          setDisconnected(true);
          void queryClient.invalidateQueries({ queryKey: ["run", id] });
          await pause(RETRY_DELAY_MS, signal);
          if (terminal()) return;
        }
      }
    },
    [queryClient],
  );

  /** Adopt a freshly started run and begin streaming it. */
  const begin = useCallback(
    (started: RunOut) => {
      openRef.current?.abort();
      const open = new AbortController();
      openRef.current = open;
      setLines([]);
      setLastLogLine(null);
      setProgress(null);
      setDisconnected(false);
      setRunId(started.id);
      queryClient.setQueryData<RunOut>(["run", started.id], started);
      void follow(started.id, open);
    },
    [queryClient, follow],
  );

  const active = run !== null && !TERMINAL_STATES.has(run.state);
  const failed = run?.state === "failed";

  return { run, runId, lines, lastLogLine, progress, disconnected, begin, active, failed };
}
