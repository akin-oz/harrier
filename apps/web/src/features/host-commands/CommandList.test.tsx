import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { stubApi } from "../../shared/testing/stubApi";
import { CommandList } from "./CommandList";
import type { CommandPlaces } from "./useCommandPlaces";

/**
 * The three-way list renders from the API (spec 096). The commands below
 * are invented on purpose: if the page carried its own copy of the list,
 * these would not appear and the real ones would.
 */

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const PLACES: CommandPlaces = {
  routed: [
    { command: "invented-routed", route: "POST /invented", note: "A note from the server." },
  ],
  host: [
    {
      command: "invented-host",
      shown: "harrier invented-host --to <directory>",
      reason: "A reason from the server.",
    },
  ],
  terminal: [{ command: "invented-terminal", reason: "Only the terminal, says the server." }],
  panel: [],
};

test("the list is the API's, in its three places", async () => {
  const calls = stubApi((call) =>
    call.path === "/api/settings/commands" ? { status: 200, body: PLACES } : undefined,
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <CommandList />
    </QueryClientProvider>,
  );

  const host = (await screen.findByRole("heading", { name: "Run on the host (1)" })).closest(
    "section",
  );
  if (host === null) throw new Error("no host section");
  expect(within(host).getByText("harrier invented-host --to <directory>")).toBeDefined();
  expect(within(host).getByRole("button", { name: /^Copy harrier invented-host/ })).toBeDefined();
  expect(within(host).getByText("A reason from the server.")).toBeDefined();

  const terminal = screen.getByRole("heading", { name: "Terminal only (1)" }).closest("section");
  if (terminal === null) throw new Error("no terminal section");
  expect(within(terminal).getByText("harrier invented-terminal")).toBeDefined();
  expect(within(terminal).getByText("Only the terminal, says the server.")).toBeDefined();
  // Nothing terminal-only gets a control of any kind.
  expect(within(terminal).queryAllByRole("button")).toHaveLength(0);

  expect(screen.getByText("POST /invented")).toBeDefined();
  expect(screen.getByText("A note from the server.")).toBeDefined();
  expect(screen.queryByText(/restore/)).toBeNull();

  expect(calls.find((call) => call.path === "/api/settings/commands")?.token).toBe("test-token");
});
