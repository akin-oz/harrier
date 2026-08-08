import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { RunPanel } from "./RunPanel";

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  onmessage: ((message: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  close(): void {
    this.closed = true;
  }

  emit(payload: Record<string, unknown>): void {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(payload) }));
  }
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  FakeEventSource.instances = [];
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

function stubStartResponse(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify(RUN), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    ),
  );
}

function renderPanel(): void {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <RunPanel createEventSource={(url) => new FakeEventSource(url) as unknown as EventSource} />
    </QueryClientProvider>,
  );
}

test("start subscribes to events and renders streamed lines until terminal", async () => {
  stubStartResponse();
  const user = userEvent.setup();
  renderPanel();

  await user.click(screen.getByRole("button", { name: "Start demo run" }));
  await waitFor(() => {
    expect(FakeEventSource.instances).toHaveLength(1);
  });
  const source = FakeEventSource.instances[0];
  if (source === undefined) {
    throw new Error("no EventSource created");
  }
  expect(source.url).toBe("/api/runs/abc123/events");

  source.emit({ type: "state_change", state: "running", exit_code: null });
  source.emit({ type: "progress", step: 1, total: 8, message: "step 1" });
  source.emit({ type: "log_line", line: "working on step 1 of 8" });

  await waitFor(() => {
    expect(screen.getByLabelText("run log").textContent).toContain("progress 1/8: step 1");
  });
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
  const user = userEvent.setup();
  renderPanel();

  await user.click(screen.getByRole("button", { name: "Start demo run" }));
  await waitFor(() => {
    expect(FakeEventSource.instances).toHaveLength(1);
  });
  const source = FakeEventSource.instances[0];
  if (source === undefined) {
    throw new Error("no EventSource created");
  }
  source.emit({ type: "state_change", state: "running", exit_code: null });
  await waitFor(() => {
    expect(screen.getByText("running")).toBeDefined();
  });

  // The server now reports the run failed; the SSE connection then breaks.
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify({ ...RUN, state: "failed", exit_code: 1 }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    ),
  );
  source.onerror?.();

  await waitFor(() => {
    expect(screen.getByText("failed")).toBeDefined();
  });
  expect(screen.getByRole("button", { name: "Start demo run" })).toHaveProperty("disabled", false);
});
