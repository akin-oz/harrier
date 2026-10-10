import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { stubApi } from "../../shared/testing/stubApi";
import { ExportSection } from "./ExportSection";

/**
 * The export as two downloads (spec 096): fetched with the token in the
 * header and never in the URL, for the selected track, with contacts on the
 * default track only, and saved by the browser. Invented rows only.
 */

const CSV = "company,title\nExample Co,Staff Engineer\n";

let saved: string[] = [];

beforeEach(() => {
  saved = [];
  window.history.replaceState(null, "", "/");
  vi.stubGlobal(
    "URL",
    Object.assign(URL, {
      createObjectURL: vi.fn(() => "blob:download"),
      revokeObjectURL: vi.fn(),
    }),
  );
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    saved.push(this.download);
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
});

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <ExportSection />
    </QueryClientProvider>,
  );
}

test("each file is fetched with the token in the header, never the URL, and saved", async () => {
  const calls = stubApi((call) =>
    call.path.startsWith("/api/ops/export/") ? { status: 200, body: CSV } : undefined,
  );
  renderSection();
  const user = userEvent.setup();

  await user.click(screen.getByRole("button", { name: "Download jobs.csv" }));
  await screen.findByText("jobs.csv downloaded.");
  await user.click(screen.getByRole("button", { name: "Download contacts.csv" }));
  await screen.findByText("contacts.csv downloaded.");

  const downloads = calls.filter((call) => call.path.startsWith("/api/ops/export/"));
  expect(downloads.map((call) => call.path)).toEqual([
    "/api/ops/export/jobs.csv",
    "/api/ops/export/contacts.csv",
  ]);
  for (const call of downloads) expect(call.token).toBe("test-token");
  expect(saved).toEqual(["jobs.csv", "contacts.csv"]);
});

test("on another track, jobs are that track's and contacts are not offered", async () => {
  window.history.replaceState(null, "", "/?track=second-search");
  const asked: string[] = [];
  stubApi((call) => {
    if (call.path.startsWith("/api/ops/export/")) {
      asked.push(call.path);
      return { status: 200, body: CSV };
    }
    return undefined;
  });
  const fetched = vi.mocked(globalThis.fetch);
  renderSection();
  const user = userEvent.setup();

  expect(
    screen.getByRole("button", { name: "Download contacts.csv" }).hasAttribute("disabled"),
  ).toBe(true);
  expect(screen.getByText(/download on the default track/)).toBeDefined();

  await user.click(screen.getByRole("button", { name: "Download jobs.csv" }));
  await waitFor(() => {
    expect(asked).toEqual(["/api/ops/export/jobs.csv"]);
  });
  const urls = fetched.mock.calls.map(([input]) =>
    input instanceof Request ? input.url : String(input),
  );
  const download = urls.find((url) => url.includes("/ops/export/jobs.csv")) ?? "";
  expect(new URL(download).searchParams.get("track")).toBe("second-search");
  expect(download).not.toContain("token");
});

test("a refusal is shown in the server's words", async () => {
  stubApi((call) =>
    call.path === "/api/ops/export/jobs.csv"
      ? { status: 409, body: { detail: "export is not available on track gone" } }
      : undefined,
  );
  renderSection();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Download jobs.csv" }));
  expect((await screen.findByRole("alert")).textContent).toBe(
    "export is not available on track gone",
  );
  expect(saved).toEqual([]);
});
