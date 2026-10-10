import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { stubApi } from "../../shared/testing/stubApi";
import { DiscoveryEditor } from "./DiscoveryEditor";
import type { ConfigEntry } from "./useKindEditor";

/** The discovery settings, with the Apify count bounded (specs 035, 096). */

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const FROM_FILE: ConfigEntry = {
  kind: "discovery",
  value: { _comment: "a note for a reader", apify_scheduled_count: 40, other_setting: true },
  source: "file",
  updated_at: null,
  error: null,
};

function renderDiscovery() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <DiscoveryEditor entry={FROM_FILE} />
    </QueryClientProvider>,
  );
}

test("a count outside the bound is not sent, and says why", async () => {
  const calls = stubApi(() => undefined);
  renderDiscovery();
  const user = userEvent.setup();
  const field = screen.getByLabelText("Scheduled Apify count per search");
  await user.clear(field);
  await user.type(field, "501");
  await user.click(screen.getByRole("button", { name: "Save discovery settings" }));

  expect(screen.getByText("The count is a whole number from 1 to 500.")).toBeDefined();
  expect(field.getAttribute("aria-invalid")).toBe("true");
  expect(calls.some((call) => call.method === "PUT")).toBe(false);
});

test("a count inside the bound saves, keeping every other setting and dropping notes", async () => {
  const calls = stubApi((call) =>
    call.method === "PUT"
      ? {
          status: 200,
          body: { ...FROM_FILE, source: "store", value: (call.body as { value: unknown }).value },
        }
      : undefined,
  );
  renderDiscovery();
  expect(screen.getByText("Kept as they are: other_setting.")).toBeDefined();
  const user = userEvent.setup();
  const field = screen.getByLabelText("Scheduled Apify count per search");
  await user.clear(field);
  await user.type(field, "500");
  await user.click(screen.getByRole("button", { name: "Save discovery settings" }));

  await screen.findByText("Saved to the store.");
  expect(calls.find((call) => call.method === "PUT")?.body).toEqual({
    value: { other_setting: true, apify_scheduled_count: 500 },
  });
});
