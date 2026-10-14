import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { holdTimers, installRunStreams } from "../../shared/testing/runStreams";
import type { RunStreams } from "../../shared/testing/runStreams";
import { RunPanel } from "./RunPanel";
import { RETRY_DELAY_MS } from "./useRunStream";

let streams: RunStreams;
// Every path the page asked the API for, in order.
let asked: string[] = [];

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const RUN = {
  id: "abc123",
  kind: "demo",
  state: "queued",
  created_at: "t",
  started_at: null,
  ended_at: null,
  exit_code: null,
};

function answer(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function stubStartResponse(): void {
  asked = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      asked.push(new URL(input instanceof Request ? input.url : String(input)).pathname);
      return Promise.resolve(answer(RUN));
    }),
  );
}

function renderPanel(): void {
  streams = installRunStreams();
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <RunPanel />
    </QueryClientProvider>,
  );
}

async function startRun(): Promise<ReturnType<typeof userEvent.setup>> {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Start demo run" }));
  await waitFor(() => {
    expect(streams.connections).toHaveLength(1);
  });
  return user;
}

test("start subscribes to events and renders streamed lines until terminal", async () => {
  stubStartResponse();
  renderPanel();

  const user = await startRun();
  const source = streams.last;
  // Local mode: the stream route under /api, and no bearer token.
  expect(source.path).toBe("/api/runs/abc123/events");
  expect(source.authorization).toBeNull();

  source.emit({ type: "state_change", state: "running", exit_code: null });
  source.emit({ type: "progress", step: 1, total: 8, message: "step 1" });
  source.emit({ type: "log_line", line: "working on step 1 of 8" });

  // Collapsed by default: the newest line is visible, the full log is not.
  await waitFor(() => {
    expect(screen.getByText("working on step 1 of 8")).toBeDefined();
  });
  expect(screen.queryByLabelText("run log")).toBeNull();

  await user.click(screen.getByRole("button", { name: "Show log" }));
  expect(screen.getByLabelText("run log").textContent).toContain("progress 1/8: step 1");
  expect(screen.getByLabelText("run log").textContent).toContain("working on step 1 of 8");
  expect(screen.getByText("running")).toBeDefined();

  source.emit({ type: "state_change", state: "succeeded", exit_code: 0 });
  await waitFor(() => {
    expect(screen.getByText("succeeded")).toBeDefined();
  });
  expect(source.closed).toBe(true);
  expect(screen.getByRole("button", { name: "Start demo run" })).toHaveProperty("disabled", false);
});

test("a broken stream refetches server truth instead of staying active", async () => {
  stubStartResponse();
  renderPanel();
  await startRun();
  const source = streams.last;
  source.emit({ type: "state_change", state: "running", exit_code: null });
  await waitFor(() => {
    expect(screen.getByText("running")).toBeDefined();
  });

  // The server now reports the run failed; the stream then breaks.
  streams.fallback = () => Promise.resolve(answer({ ...RUN, state: "failed", exit_code: 1 }));
  source.fail();

  await waitFor(() => {
    expect(screen.getByText("failed")).toBeDefined();
  });
  expect(screen.getByRole("button", { name: "Start demo run" })).toHaveProperty("disabled", false);
});

test("the log is collapsed on first paint and the toggle reports its state", async () => {
  stubStartResponse();
  renderPanel();
  const user = await startRun();
  streams.last.emit({ type: "log_line", line: "first line" });

  await waitFor(() => {
    expect(screen.getByText("first line")).toBeDefined();
  });
  const toggle = screen.getByRole("button", { name: "Show log" });
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  expect(screen.queryByLabelText("run log")).toBeNull();

  await user.click(toggle);
  const opened = screen.getByRole("button", { name: "Hide log" });
  expect(opened.getAttribute("aria-expanded")).toBe("true");
  expect(screen.getByLabelText("run log")).toBeDefined();
});

test("lines arriving while collapsed update the visible last line without expanding", async () => {
  stubStartResponse();
  renderPanel();
  await startRun();
  streams.last.emit({ type: "log_line", line: "older line" });
  streams.last.emit({ type: "log_line", line: "newest line" });

  await waitFor(() => {
    expect(screen.getByText("newest line")).toBeDefined();
  });
  expect(screen.queryByText("older line")).toBeNull();
  expect(screen.queryByLabelText("run log")).toBeNull();
  expect(screen.getByRole("button", { name: "Show log" }).getAttribute("aria-expanded")).toBe(
    "false",
  );
});

test("a failed run opens the log without being asked", async () => {
  // The one case where the log is the point (spec 026).
  stubStartResponse();
  renderPanel();
  await startRun();
  streams.last.emit({ type: "log_line", line: "traceback: boom" });
  streams.last.emit({ type: "state_change", state: "failed", exit_code: 1 });

  await waitFor(() => {
    expect(screen.getByLabelText("run log")).toBeDefined();
  });
  expect(screen.getByRole("button", { name: "Hide log" }).getAttribute("aria-expanded")).toBe(
    "true",
  );
});

test("a started run with no lines yet says it is waiting", async () => {
  stubStartResponse();
  renderPanel();
  await startRun();
  streams.last.emit({ type: "state_change", state: "running", exit_code: null });

  await waitFor(() => {
    expect(screen.getByText("waiting for the first line…")).toBeDefined();
  });
});

test("a dropped stream says so instead of looking stuck", async () => {
  stubStartResponse();
  renderPanel();
  await startRun();
  streams.last.fail();

  await waitFor(() => {
    expect(screen.getByRole("status").textContent).toContain("Lost the log stream");
  });
});

test("a stream ended at token expiry resumes where it stopped", async () => {
  stubStartResponse();
  renderPanel();
  const user = await startRun();
  const first = streams.last;
  first.emit({ type: "state_change", state: "running", exit_code: null });
  first.emit({ type: "log_line", line: "before expiry" });
  // The hosted API ends the stream with a comment when the token expires
  // (spec 132); the run is still going.
  first.raw(": token expired\n\n");
  await waitFor(() => {
    expect(screen.getByText("before expiry")).toBeDefined();
  });
  first.end();

  await waitFor(() => {
    expect(streams.connections).toHaveLength(2);
  });
  const second = streams.last;
  // The id of the last event it received, which was the second.
  expect(second.lastEventId).toBe("2");
  // A server that replays an event it already sent is not shown twice.
  second.raw('id: 2\ndata: {"type":"log_line","line":"before expiry"}\n\n');
  second.emit({ type: "log_line", line: "after expiry" });
  await waitFor(() => {
    expect(screen.getByText("after expiry")).toBeDefined();
  });

  await user.click(screen.getByRole("button", { name: "Show log" }));
  expect(screen.getByLabelText("run log").textContent).toBe(
    ["run is running", "before expiry", "after expiry"].join("\n"),
  );
});

test("a stream that keeps ending waits before reconnecting", async () => {
  stubStartResponse();
  const timers = holdTimers(RETRY_DELAY_MS);
  renderPanel();
  await startRun();

  // One empty end reconnects at once.
  streams.last.end();
  await waitFor(() => {
    expect(streams.connections).toHaveLength(2);
  });
  expect(timers.waiting).toBe(0);

  // A second empty end in a row waits the retry delay.
  streams.last.end();
  await waitFor(() => {
    expect(timers.waiting).toBe(1);
  });
  expect(streams.connections).toHaveLength(2);

  timers.release();
  await waitFor(() => {
    expect(streams.connections).toHaveLength(3);
  });
});

test("a stream that delivered something is reconnected at once", async () => {
  stubStartResponse();
  const timers = holdTimers(RETRY_DELAY_MS);
  renderPanel();
  await startRun();

  // An end after events is the token's expiry, not a server refusing to
  // stream: reconnect at once, and one empty end after it does too.
  streams.last.emit({ type: "log_line", line: "something" });
  await waitFor(() => {
    expect(screen.getByText("something")).toBeDefined();
  });
  streams.last.end();
  await waitFor(() => {
    expect(streams.connections).toHaveLength(2);
  });
  streams.last.end();
  await waitFor(() => {
    expect(streams.connections).toHaveLength(3);
  });
  expect(timers.waiting).toBe(0);

  // Only the second empty end in a row waits.
  streams.last.end();
  await waitFor(() => {
    expect(timers.waiting).toBe(1);
  });
  expect(streams.connections).toHaveLength(3);
});

test("a recovered stream stops saying it is disconnected", async () => {
  stubStartResponse();
  const timers = holdTimers(RETRY_DELAY_MS);
  renderPanel();
  await startRun();
  streams.last.emit({ type: "log_line", line: "alive" });
  await waitFor(() => {
    expect(screen.getByText("alive")).toBeDefined();
  });
  const before = asked.filter((path) => path === "/api/runs/abc123").length;

  streams.last.fail();
  await waitFor(() => {
    expect(screen.getByRole("status").textContent).toContain("Lost the log stream");
  });
  // The run is read again, and the hook waits before it retries.
  await waitFor(() => {
    expect(asked.filter((path) => path === "/api/runs/abc123").length).toBeGreaterThan(before);
  });
  expect(timers.waiting).toBe(1);
  expect(streams.connections).toHaveLength(1);

  timers.release();
  await waitFor(() => {
    expect(streams.connections).toHaveLength(2);
  });
  expect(streams.last.lastEventId).toBe("1");
  // Still disconnected until an event arrives on the new connection.
  expect(screen.getByRole("status")).toBeDefined();
  streams.last.emit({ type: "log_line", line: "back" });
  await waitFor(() => {
    expect(screen.queryByRole("status")).toBeNull();
  });
});

test("a run the stream does not know stops the reconnecting", async () => {
  stubStartResponse();
  const timers = holdTimers(RETRY_DELAY_MS);
  renderPanel();
  const user = userEvent.setup();
  streams.refuseNext = { status: 404, body: { detail: "no such run" } };
  await user.click(screen.getByRole("button", { name: "Start demo run" }));
  await waitFor(() => {
    expect(streams.requests).toBe(1);
  });
  await new Promise((resolve) => setTimeout(resolve, 50));
  expect(timers.waiting).toBe(0);
  expect(streams.requests).toBe(1);
  expect(screen.queryByRole("status")).toBeNull();
});
