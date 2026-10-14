import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { installRunStreams } from "../../shared/testing/runStreams";
import type { RunStreams } from "../../shared/testing/runStreams";
import { useRunStream } from "./useRunStream";
import type { RunOut } from "./useRunStream";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function run(id: string): RunOut {
  return {
    id,
    kind: "demo",
    state: "running",
    created_at: "t",
    started_at: null,
    ended_at: null,
    exit_code: null,
  };
}

// A component that does nothing but follow runs the way a page does.
function Probe() {
  const stream = useRunStream();
  return (
    <div>
      <button
        type="button"
        onClick={() => {
          stream.begin(run("r1"));
        }}
      >
        follow r1
      </button>
      <button
        type="button"
        onClick={() => {
          stream.begin(run("r2"));
        }}
      >
        follow r2
      </button>
      <p>{stream.lines.join("|")}</p>
    </div>
  );
}

function mount(): { streams: RunStreams; unmount: () => void } {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const path = new URL(input instanceof Request ? input.url : String(input)).pathname;
      return Promise.resolve(
        new Response(JSON.stringify(run(path.split("/")[3] ?? "r1")), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    }),
  );
  const streams = installRunStreams();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <Probe />
    </QueryClientProvider>,
  );
  return { streams, unmount: view.unmount };
}

test("a stream is aborted when it is no longer watched", async () => {
  const user = userEvent.setup();
  const { streams, unmount } = mount();

  // A terminal state ends the stream.
  await user.click(screen.getByRole("button", { name: "follow r1" }));
  await waitFor(() => {
    expect(streams.connections).toHaveLength(1);
  });
  const first = streams.last;
  expect(first.closed).toBe(false);
  act(() => {
    first.emit({ type: "state_change", state: "succeeded", exit_code: 0 });
  });
  await waitFor(() => {
    expect(first.closed).toBe(true);
  });

  // Beginning another run ends the one before it.
  await user.click(screen.getByRole("button", { name: "follow r1" }));
  await waitFor(() => {
    expect(streams.connections).toHaveLength(2);
  });
  const second = streams.last;
  await user.click(screen.getByRole("button", { name: "follow r2" }));
  await waitFor(() => {
    expect(streams.connections).toHaveLength(3);
  });
  expect(second.closed).toBe(true);
  const third = streams.last;
  expect(third.runId).toBe("r2");
  expect(third.closed).toBe(false);

  // So does going away.
  unmount();
  expect(third.closed).toBe(true);
  expect(streams.connections).toHaveLength(3);
});
