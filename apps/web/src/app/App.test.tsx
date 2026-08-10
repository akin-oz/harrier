import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { App } from "./App";

afterEach(() => {
  vi.unstubAllGlobals();
});

test("renders the shell and the tracker fed by /jobs", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify([]), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    ),
  );
  render(<App />);
  expect(screen.getByRole("heading", { name: "Harrier" })).toBeDefined();
  await waitFor(() => {
    expect(screen.getByText("No jobs yet. Run discovery to find some.")).toBeDefined();
  });
});
