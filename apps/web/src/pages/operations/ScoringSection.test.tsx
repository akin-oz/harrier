import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { installRunStreams } from "../../shared/testing/runStreams";
import { TEST_TOKEN, stubApi } from "../../shared/testing/stubApi";
import { ScoringSection } from "./ScoringSection";

/**
 * The learned score's section (spec 157): the export and training are two
 * buttons, and training posts `activate: true`, so the gate decides whether
 * the model replaces the rules. Its output is the CLI's own lines.
 */

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function run(id: string, kind: string) {
  return {
    id,
    kind,
    state: "running",
    created_at: "2026-10-17T08:00:00Z",
    started_at: null,
    ended_at: null,
    exit_code: null,
  };
}

test("trains the model and streams the run", async () => {
  const calls = stubApi((call) => {
    if (call.path === "/api/ops/scoring/train") {
      return { status: 200, body: run("train1", "scoring-train") };
    }
    if (call.path.startsWith("/api/runs/")) return { status: 200, body: run("train1", "any") };
    return undefined;
  });
  const streams = installRunStreams();
  const user = userEvent.setup();
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <ScoringSection />
    </QueryClientProvider>,
  );

  expect(screen.getByRole("button", { name: "Write the feature export" })).toBeTruthy();
  expect(screen.getByText(/activates the model only if it beats the rules/)).toBeTruthy();
  expect(screen.queryByText(/runs on the host/)).toBeNull();

  await user.click(screen.getByRole("button", { name: "Train the model" }));
  await waitFor(() => {
    expect(streams.connections).toHaveLength(1);
  });
  const posted = calls.filter((call) => call.method === "POST");
  expect(posted.map((call) => call.path)).toEqual(["/api/ops/scoring/train"]);
  expect(posted[0]?.body).toEqual({ activate: true });
  expect(posted[0]?.token).toBe(TEST_TOKEN);
  expect(streams.last.runId).toBe("train1");

  act(() => {
    streams.last.emit({ type: "log_line", line: "activated model 0a1b2c3d" });
    streams.last.emit({ type: "state_change", state: "succeeded", exit_code: 0 });
  });
  const status = await screen.findByText(/Model training:/);
  await waitFor(() => {
    expect(within(status).getByText("succeeded")).toBeTruthy();
  });
  expect(screen.getByLabelText("Model training log").textContent).toContain(
    "activated model 0a1b2c3d",
  );
  expect(screen.getByRole("button", { name: "Train the model" })).toHaveProperty("disabled", false);
});
