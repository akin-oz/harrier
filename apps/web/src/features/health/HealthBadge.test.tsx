import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { HealthBadge } from "./HealthBadge";

afterEach(() => {
  vi.unstubAllGlobals();
});

function healthResponse(overrides: Record<string, unknown>) {
  return vi.fn(() =>
    Promise.resolve(
      new Response(
        JSON.stringify({
          name: "harrier",
          version: "0.1.0",
          demo: false,
          database: "/app/data/tracker.db",
          job_count: 3,
          revision: "unknown",
          built_at: "unknown",
          ...overrides,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    ),
  );
}

function renderBadge() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <HealthBadge />
    </QueryClientProvider>,
  );
}

// The point of the field: a container serving older code than the checkout is
// visible to the operator rather than only to whoever thinks to curl /health
// (spec 051).
test("shows the revision the running process was built from", async () => {
  vi.stubGlobal("fetch", healthResponse({ revision: "b246cc6", built_at: "2026-08-16T12:57:41Z" }));
  renderBadge();
  await waitFor(() => {
    expect(screen.getByText("b246cc6")).toBeDefined();
  });
  expect(screen.getByTitle("built 2026-08-16T12:57:41Z")).toBeDefined();
});

// `just dev` is not built from an image and has no revision. Reporting "dev"
// is the honest label; rendering the literal "unknown" would read as a fault.
test("an unstamped process reads as dev rather than as a fault", async () => {
  vi.stubGlobal("fetch", healthResponse({}));
  renderBadge();
  await waitFor(() => {
    expect(screen.getByText("dev")).toBeDefined();
  });
  expect(screen.getByTitle("not built from an image")).toBeDefined();
  expect(screen.queryByText("unknown")).toBeNull();
});
