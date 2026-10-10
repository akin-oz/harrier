import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { stubApi } from "../../shared/testing/stubApi";
import type { ApiCall } from "../../shared/testing/stubApi";
import { ConfigSection } from "./ConfigSection";

/**
 * The configuration section (spec 096): one editor per kind over `/config`,
 * read with the token (spec 097), and `config import` behind a confirmation
 * that says it overwrites. Invented values only (ADR-008).
 */

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function kinds(feeds: string[]) {
  return [
    {
      kind: "feeds",
      value: feeds,
      source: "store",
      updated_at: "2026-01-01 00:00:00",
      error: null,
    },
    { kind: "linkedin_searches", value: [], source: "file", updated_at: null, error: null },
    { kind: "discovery", value: {}, source: "file", updated_at: null, error: null },
    { kind: "company_holds", value: [], source: "file", updated_at: null, error: null },
    {
      kind: "academic_searches",
      value: { "example-track": { query: "example" } },
      source: "store",
      updated_at: "2026-01-01 00:00:00",
      error: null,
    },
  ];
}

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ConfigSection />
    </QueryClientProvider>,
  );
}

function handler(state: { imported: boolean }) {
  return (call: ApiCall) => {
    if (call.path === "/api/config" && call.method === "GET") {
      return {
        status: 200,
        body: kinds(
          state.imported ? ["https://jobs.lever.co/fromfile"] : ["https://jobs.lever.co/before"],
        ),
      };
    }
    if (call.path === "/api/config/import") {
      state.imported = true;
      return {
        status: 200,
        body: {
          imported: [{ kind: "feeds", count: 1, unit: "entries" }],
          skipped: ["linkedin_searches", "company_holds"],
          report: [
            "feeds: 1 entries imported",
            "linkedin_searches: no file to import, skipped",
            "company_holds: no file to import, skipped",
          ],
          total: 5,
        },
      };
    }
    if (call.path === "/api/settings/feeds/routing") return { status: 200, body: { unrouted: [] } };
    return undefined;
  };
}

test("every kind has its editor, and a kind without one is shown read-only", async () => {
  const calls = stubApi(handler({ imported: false }));
  renderSection();
  await screen.findByRole("heading", { name: "Board watchlist" });
  for (const name of ["LinkedIn searches", "Discovery settings", "Company holds"]) {
    expect(screen.getByRole("heading", { name })).toBeDefined();
  }
  expect(screen.getByRole("heading", { name: "academic searches" })).toBeDefined();
  expect(screen.getByText(/Read-only here/)).toBeDefined();
  expect(screen.queryByRole("button", { name: "Save academic searches" })).toBeNull();

  const read = calls.find((call) => call.path === "/api/config");
  expect(read?.token).toBe("test-token");
});

test("import asks first, reports in the command's lines, and reloads every editor", async () => {
  const calls = stubApi(handler({ imported: false }));
  renderSection();
  const field = await screen.findByLabelText("Board watchlist: One URL per line", {
    selector: "textarea",
  });
  expect((field as HTMLTextAreaElement).value).toBe("https://jobs.lever.co/before");

  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Import from the files" }));
  expect(
    screen.getByRole("group", { name: "Import the configuration files" }).textContent,
  ).toContain("overwritten");
  expect(calls.some((call) => call.path === "/api/config/import")).toBe(false);

  await user.click(screen.getByRole("button", { name: "Overwrite from the files" }));
  await screen.findByText("Imported 1 of 5 kinds.");
  expect(screen.getByText("linkedin_searches: no file to import, skipped")).toBeDefined();
  const reloaded = await screen.findAllByDisplayValue("https://jobs.lever.co/fromfile");
  expect(reloaded).toHaveLength(1);
});

test("an import with nothing to import says so in the server's words", async () => {
  stubApi((call) => {
    if (call.path === "/api/config/import") {
      return { status: 409, body: { detail: "nothing to import; no configuration files found" } };
    }
    return handler({ imported: false })(call);
  });
  renderSection();
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Import from the files" }));
  await user.click(screen.getByRole("button", { name: "Overwrite from the files" }));
  expect((await screen.findByRole("alert")).textContent).toBe(
    "nothing to import; no configuration files found",
  );
});

test("the import question is answered from the keyboard, and focus comes back", async () => {
  const calls = stubApi(handler({ imported: false }));
  renderSection();
  const user = userEvent.setup();
  const opener = await screen.findByRole("button", { name: "Import from the files" });

  opener.focus();
  await user.keyboard("{Enter}");
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Cancel" }));
  await user.keyboard("{Escape}");

  expect(document.activeElement).toBe(
    screen.getByRole("button", { name: "Import from the files" }),
  );
  expect(calls.some((call) => call.path === "/api/config/import")).toBe(false);
});
