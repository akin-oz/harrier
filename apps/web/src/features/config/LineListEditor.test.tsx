import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { stubApi as stub } from "../../shared/testing/stubApi";
import { LineListEditor } from "./LineListEditor";
import type { ConfigEntry } from "./useKindEditor";

/**
 * The line-list editors over `/config/{kind}` (spec 096): save the whole
 * kind in one request, refuse in the store's words keeping the input, show
 * where the value came from, and reset to the file behind a confirmation.
 * Invented boards only (ADR-008).
 */

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function entry(overrides: Partial<ConfigEntry> = {}): ConfigEntry {
  return {
    kind: "feeds",
    value: ["https://boards.greenhouse.io/example"],
    source: "store",
    updated_at: "2026-01-02 03:04:05",
    error: null,
    ...overrides,
  };
}

function renderEditor(value: ConfigEntry) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <LineListEditor entry={value} title="Board watchlist" description="Boards." />
    </QueryClientProvider>,
  );
}

test("saves the whole kind in one request and says where it came from", async () => {
  const calls = stub((call) => {
    if (call.method === "PUT") {
      return {
        status: 200,
        body: entry({ value: call.body && (call.body as { value: unknown }).value }),
      };
    }
    if (call.path === "/api/settings/feeds/routing") return { status: 200, body: { unrouted: [] } };
    return undefined;
  });
  renderEditor(entry({ source: "file", updated_at: null }));
  expect(screen.getByText("From the file in the checkout")).toBeDefined();

  const user = userEvent.setup();
  const field = screen.getByLabelText("Board watchlist: One URL per line");
  await user.clear(field);
  await user.type(field, "https://jobs.lever.co/acme\n\n  https://jobs.ashbyhq.com/demo  ");
  await user.click(screen.getByRole("button", { name: "Save board watchlist" }));

  await screen.findByText("Saved to the store.");
  const puts = calls.filter((call) => call.method === "PUT");
  expect(puts).toHaveLength(1);
  expect(puts[0]?.path).toBe("/api/config/feeds");
  expect(puts[0]?.body).toEqual({
    value: ["https://jobs.lever.co/acme", "https://jobs.ashbyhq.com/demo"],
  });
  expect(puts[0]?.token).toBe("test-token");
  expect(screen.getByText(/From the store, saved 2026-01-02 03:04/)).toBeDefined();
});

test("a refusal is shown in the store's words and the input is kept", async () => {
  stub((call) => {
    if (call.method === "PUT") {
      return { status: 400, body: { detail: "feeds entries must be strings, got int" } };
    }
    if (call.path === "/api/settings/feeds/routing") return { status: 200, body: { unrouted: [] } };
    return undefined;
  });
  renderEditor(entry());
  const user = userEvent.setup();
  const field = screen.getByLabelText("Board watchlist: One URL per line");
  await user.clear(field);
  await user.type(field, "https://example.org/typed-by-hand");
  await user.click(screen.getByRole("button", { name: "Save board watchlist" }));

  expect((await screen.findByRole("alert")).textContent).toBe(
    "feeds entries must be strings, got int",
  );
  expect((field as HTMLTextAreaElement).value).toBe("https://example.org/typed-by-hand");
});

test("reset to the file asks first, and only the confirmation deletes", async () => {
  const calls = stub((call) => {
    if (call.method === "DELETE") {
      return {
        status: 200,
        body: entry({
          source: "file",
          updated_at: null,
          value: ["https://jobs.lever.co/fromfile"],
        }),
      };
    }
    if (call.path === "/api/settings/feeds/routing") return { status: 200, body: { unrouted: [] } };
    return undefined;
  });
  renderEditor(entry());
  const user = userEvent.setup();

  await user.click(screen.getByRole("button", { name: "Reset to the file" }));
  const question = screen.getByRole("group", { name: "Reset board watchlist to the file" });
  expect(question.textContent).toContain(
    "The stored value is deleted and the file in the checkout",
  );
  expect(calls.some((call) => call.method === "DELETE")).toBe(false);

  await user.click(screen.getByRole("button", { name: "Cancel" }));
  expect(calls.some((call) => call.method === "DELETE")).toBe(false);

  await user.click(screen.getByRole("button", { name: "Reset to the file" }));
  await user.click(screen.getByRole("button", { name: "Remove and use the file" }));
  await screen.findByText("The stored value was removed. The file in the checkout is in use.");
  expect(calls.filter((call) => call.method === "DELETE").map((call) => call.path)).toEqual([
    "/api/config/feeds",
  ]);
  expect(
    screen.getByLabelText<HTMLTextAreaElement>("Board watchlist: One URL per line").value,
  ).toBe("https://jobs.lever.co/fromfile");
});

test("resetting a kind nothing stored says the file was already in use", async () => {
  stub((call) => {
    if (call.method === "DELETE") return { status: 200, body: entry({ source: "file" }) };
    if (call.path === "/api/settings/feeds/routing") return { status: 200, body: { unrouted: [] } };
    return undefined;
  });
  renderEditor(entry({ source: "file", updated_at: null }));
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Reset to the file" }));
  await user.click(screen.getByRole("button", { name: "Remove and use the file" }));
  await screen.findByText("Nothing was stored. The file in the checkout was already in use.");
});

test("a line no importer handles is named in the server's words and does not block the save", async () => {
  const calls = stub((call) => {
    if (call.path === "/api/settings/feeds/routing") {
      const urls = (call.body as { urls: string[] }).urls;
      return {
        status: 200,
        body: {
          unrouted: urls
            .filter((url) => url.includes("careers.example"))
            .map((url) => ({ url, message: `no importer handles ${url}` })),
        },
      };
    }
    if (call.method === "PUT") return { status: 200, body: entry() };
    return undefined;
  });
  renderEditor(entry({ value: ["https://careers.example.com/jobs"] }));

  await screen.findByText("no importer handles https://careers.example.com/jobs");
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Save board watchlist" }));
  await waitFor(() => {
    expect(calls.some((call) => call.method === "PUT")).toBe(true);
  });
});

test("the searches editor asks nothing about importers", async () => {
  const calls = stub(() => undefined);
  renderEditor(entry({ kind: "linkedin_searches", value: ["https://example.org/search"] }));
  await new Promise((resolve) => setTimeout(resolve, 10));
  expect(calls.some((call) => call.path === "/api/settings/feeds/routing")).toBe(false);
});

test("the reset question is answered from the keyboard, and focus comes back", async () => {
  const calls = stub(() => undefined);
  renderEditor(entry());
  const user = userEvent.setup();
  const opener = screen.getByRole("button", { name: "Reset to the file" });

  opener.focus();
  await user.keyboard("{Enter}");
  // Focus lands on Cancel, the answer that changes nothing.
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Cancel" }));
  await user.keyboard("{Escape}");

  expect(screen.queryByRole("group", { name: "Reset board watchlist to the file" })).toBeNull();
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Reset to the file" }));
  expect(calls.some((call) => call.method === "DELETE")).toBe(false);
});
