import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { stubApi } from "../../shared/testing/stubApi";
import { HoldsEditor } from "./HoldsEditor";
import type { ConfigEntry } from "./useKindEditor";

/**
 * The company holds (spec 096, spec 052): one row per company with an
 * optional last day, and an expired hold shown as expired rather than
 * dropped, because expiry is decided when holds are read. Invented
 * companies only (ADR-008).
 */

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const HOLDS: ConfigEntry = {
  kind: "company_holds",
  value: [
    "Forever Co",
    { company: "Lapsed Co", hold_until: "2026-03-01" },
    { company: "Held Co", hold_until: "2026-03-10" },
  ],
  source: "store",
  updated_at: "2026-01-01 00:00:00",
  error: null,
};

function renderHolds() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <HoldsEditor entry={HOLDS} today="2026-03-05" />
    </QueryClientProvider>,
  );
}

function rowOf(company: string): HTMLElement {
  const input = screen.getByLabelText(`Company, ${company}`);
  const row = input.closest("tr");
  if (row === null) throw new Error(`no row for ${company}`);
  return row;
}

test("an expired hold is shown as expired, a dated one as active, an undated one as open", () => {
  stubApi(() => undefined);
  renderHolds();
  expect(within(rowOf("Lapsed Co")).getByText("Expired")).toBeDefined();
  expect(within(rowOf("Held Co")).getByText("Active")).toBeDefined();
  expect(within(rowOf("Forever Co")).getByText("No end")).toBeDefined();
});

test("a hold applies on its last day", () => {
  stubApi(() => undefined);
  const client = new QueryClient();
  render(
    <QueryClientProvider client={client}>
      <HoldsEditor entry={HOLDS} today="2026-03-10" />
    </QueryClientProvider>,
  );
  expect(within(rowOf("Held Co")).getByText("Active")).toBeDefined();
});

test("saves every hold in the store's two shapes, expired ones included", async () => {
  const calls = stubApi((call) =>
    call.method === "PUT"
      ? { status: 200, body: { ...HOLDS, value: (call.body as { value: unknown }).value } }
      : undefined,
  );
  renderHolds();
  const user = userEvent.setup();

  await user.click(screen.getByRole("button", { name: "Remove Held Co" }));
  await user.click(screen.getByRole("button", { name: "Add a company" }));
  await user.type(screen.getByLabelText("Company, row 3"), "New Co");
  await user.click(screen.getByRole("button", { name: "Save company holds" }));

  await screen.findByText("Saved to the store.");
  const put = calls.find((call) => call.method === "PUT");
  expect(put?.path).toBe("/api/config/company_holds");
  expect(put?.body).toEqual({
    value: ["Forever Co", { company: "Lapsed Co", hold_until: "2026-03-01" }, "New Co"],
  });
});

test("a refused hold keeps the rows as typed", async () => {
  stubApi((call) =>
    call.method === "PUT"
      ? { status: 400, body: { detail: "company_holds entry needs a non-blank company" } }
      : undefined,
  );
  renderHolds();
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Company, Forever Co"), " Ltd");
  await user.click(screen.getByRole("button", { name: "Save company holds" }));
  expect((await screen.findByRole("alert")).textContent).toBe(
    "company_holds entry needs a non-blank company",
  );
  expect(screen.getByLabelText<HTMLInputElement>("Company, Forever Co Ltd").value).toBe(
    "Forever Co Ltd",
  );
});
