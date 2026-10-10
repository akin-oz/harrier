import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { stubApi } from "../../shared/testing/stubApi";
import type { ApiCall } from "../../shared/testing/stubApi";
import { SettingsPage } from "./SettingsPage";

/**
 * The Settings page (spec 096): its sections, the profile list, and backups,
 * which start runs of the CLI's own verbs and name archives rather than
 * locating them. Invented names only (ADR-008).
 */

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const ARCHIVE = "harrier-data-2026-03-01-020000.tar.gz";

function run(kind: string, state = "running") {
  return {
    id: "run7",
    kind,
    state,
    created_at: "2026-03-05T00:00:00Z",
    started_at: null,
    ended_at: null,
    exit_code: null,
  };
}

interface Options {
  directory?: "present" | "absent";
  archives?: unknown[];
  profile?: unknown[];
  runKind?: string;
  runState?: string;
}

function handler(options: Options) {
  return (call: ApiCall) => {
    switch (call.path) {
      case "/api/config":
        return { status: 200, body: [] };
      case "/api/settings/profile":
        return { status: 200, body: options.profile ?? [] };
      case "/api/settings/backups":
        if (call.method === "POST") return { status: 200, body: run("backup") };
        return {
          status: 200,
          body: { directory: options.directory ?? "present", archives: options.archives ?? [] },
        };
      case `/api/settings/backups/${ARCHIVE}/verify`:
        return { status: 200, body: run("verify-backup") };
      case "/api/runs/run7":
        return {
          status: 200,
          body: run(options.runKind ?? "backup", options.runState ?? "running"),
        };
      case "/api/settings/commands":
        return { status: 200, body: { routed: [], host: [], terminal: [], panel: [] } };
      case "/api/settings/host":
        return {
          status: 200,
          body: {
            schedule_definition: "absent",
            schedule: [],
            schedule_installed: "unknown",
            gmail_token: { state: "not_configured", age_days: null },
            model: { state: "missing", trained_at: null, version: null },
            newest_feature_export: null,
            image_revision: "unknown",
            database_owner: "unknown",
          },
        };
      default:
        return undefined;
    }
  };
}

class FakeEventSource {
  static last: FakeEventSource | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(readonly url: string) {
    FakeEventSource.last = this;
  }
  emit(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent<string>);
  }
  close(): void {
    /* nothing to release in a test */
  }
}

function renderPage(): void {
  FakeEventSource.last = null;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <SettingsPage
        createEventSource={(url) => new FakeEventSource(url) as unknown as EventSource}
      />
    </QueryClientProvider>,
  );
}

test("the page has its five sections, in the order an operator needs them", async () => {
  stubApi(handler({}));
  renderPage();
  const headings = (await screen.findAllByRole("heading", { level: 3 })).map(
    (heading) => heading.textContent,
  );
  expect(headings).toEqual([
    "Configuration",
    "Profile documents",
    "Backups",
    "What only the host can do",
    "Commands without a button",
  ]);
});

test("profile documents are listed by name", async () => {
  const calls = stubApi(
    handler({
      profile: [
        {
          kind: "truth",
          name: "example-truth",
          format: "markdown",
          updated_at: "2026-01-01 00:00:00",
        },
      ],
    }),
  );
  renderPage();
  expect(await screen.findByText("example-truth")).toBeDefined();
  expect(calls.find((call) => call.path === "/api/settings/profile")?.token).toBe("test-token");
});

test("a missing backups directory is said in words, and a backup can still be taken", async () => {
  const calls = stubApi(handler({ directory: "absent" }));
  renderPage();
  expect(await screen.findByText(/backups directory is not mounted here/)).toBeDefined();

  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Take a backup" }));
  await waitFor(() => {
    expect(
      calls.some((call) => call.method === "POST" && call.path === "/api/settings/backups"),
    ).toBe(true);
  });
  expect(FakeEventSource.last?.url).toBe("/api/runs/run7/events");
  act(() => {
    FakeEventSource.last?.emit({ type: "log_line", line: `${ARCHIVE} (0.1 MiB, verified)` });
    FakeEventSource.last?.emit({ type: "state_change", state: "succeeded", exit_code: 0 });
  });
  expect(await screen.findByText(`${ARCHIVE} (0.1 MiB, verified)`)).toBeDefined();
});

test("each listed archive can be verified by its name, and a failure is marked", async () => {
  const calls = stubApi(
    handler({
      runKind: "verify-backup",
      archives: [
        {
          name: ARCHIVE,
          size_bytes: 1048576,
          modified_at: "2026-03-01T02:00:00+00:00",
          verification: "failed",
        },
      ],
    }),
  );
  renderPage();
  const row = (await screen.findByText(ARCHIVE)).closest("tr");
  if (row === null) throw new Error("no row");
  expect(within(row).getByText("Failed verification")).toBeDefined();
  expect(within(row).getByText("1.0 MiB")).toBeDefined();

  const user = userEvent.setup();
  await user.click(within(row).getByRole("button", { name: `Verify ${ARCHIVE}` }));
  await waitFor(() => {
    expect(calls.some((call) => call.path === `/api/settings/backups/${ARCHIVE}/verify`)).toBe(
      true,
    );
  });
  act(() => {
    FakeEventSource.last?.emit({
      type: "log_line",
      line: `archive is not usable: ${ARCHIVE} is not a readable archive`,
    });
    FakeEventSource.last?.emit({ type: "state_change", state: "failed", exit_code: 1 });
  });
  expect((await screen.findByRole("alert")).textContent).toContain("is not a readable archive");
});

test("restore has no button", async () => {
  stubApi(handler({}));
  renderPage();
  await screen.findByRole("heading", { name: "Backups" });
  expect(screen.queryByRole("button", { name: /restore/i })).toBeNull();
});
