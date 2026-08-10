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

test("the log is collapsed on first paint and the toggle reports its state", async () => {
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
  source.emit({ type: "log_line", line: "first line" });

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
  source.emit({ type: "log_line", line: "older line" });
  source.emit({ type: "log_line", line: "newest line" });

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
  source.emit({ type: "log_line", line: "traceback: boom" });
  source.emit({ type: "state_change", state: "failed", exit_code: 1 });

  await waitFor(() => {
    expect(screen.getByLabelText("run log")).toBeDefined();
  });
  expect(screen.getByRole("button", { name: "Hide log" }).getAttribute("aria-expanded")).toBe(
    "true",
  );
});
