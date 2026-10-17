import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { stubApi } from "../../shared/testing/stubApi";
import type { ApiCall } from "../../shared/testing/stubApi";
import { SettingsPage } from "./SettingsPage";
import { installRunStreams } from "../../shared/testing/runStreams";
import type { RunStreams } from "../../shared/testing/runStreams";

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
      case "/api/ops/profile":
        return { status: 200, body: options.profile ?? [] };
      case "/api/ops/backup":
        return { status: 200, body: run("backup") };
      case "/api/ops/schedule":
        return {
          status: 200,
          body: {
            jobs: [],
            error: null,
            installed_state: "",
            host_command: "harrier schedule status",
          },
        };
      case "/api/settings/backups":
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

let streams: RunStreams;

function renderPage(): void {
  streams = installRunStreams();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <SettingsPage />
    </QueryClientProvider>,
  );
}

test("the page has its six sections, in the order an operator needs them", async () => {
  stubApi(handler({}));
  renderPage();
  const headings = (await screen.findAllByRole("heading", { level: 3 })).map(
    (heading) => heading.textContent,
  );
  expect(headings).toEqual([
    "Configuration",
    "Profile documents",
    "Backups",
    "Export",
    "What only the host can do",
    "Commands without a button",
  ]);
});

test("profile documents are listed by name, read with the token", async () => {
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
  expect(calls.find((call) => call.path === "/api/ops/profile")?.token).toBe("test-token");
});

test("a missing backups directory is said in words, and a backup can still be taken", async () => {
  const calls = stubApi(handler({ directory: "absent" }));
  renderPage();
  expect(await screen.findByText(/backups directory is not mounted here/)).toBeDefined();

  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Take a backup" }));
  await waitFor(() => {
    expect(calls.some((call) => call.method === "POST" && call.path === "/api/ops/backup")).toBe(
      true,
    );
  });
  // An empty body: an archive, and no older one deleted (spec 050).
  expect(calls.find((call) => call.path === "/api/ops/backup")?.body).toEqual({});
  expect(streams.last.path).toBe("/api/runs/run7/events");
  act(() => {
    streams.last.emit({ type: "log_line", line: `${ARCHIVE} (0.1 MiB, verified)` });
    streams.last.emit({ type: "state_change", state: "succeeded", exit_code: 0 });
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
    streams.last.emit({
      type: "log_line",
      line: `archive is not usable: ${ARCHIVE} is not a readable archive`,
    });
    streams.last.emit({ type: "state_change", state: "failed", exit_code: 1 });
  });
  expect((await screen.findByRole("alert")).textContent).toContain("is not a readable archive");
});

test("a finished backup is told in a status region that was already there", async () => {
  stubApi(handler({ directory: "absent" }));
  renderPage();
  const user = userEvent.setup();

  // The page's regions before the backup. A region inserted with its text
  // may never be spoken (spec 156).
  const take = await screen.findByRole("button", { name: "Take a backup" });
  const before = screen.getAllByRole("status");
  await user.click(take);
  await waitFor(() => {
    expect(streams.connections.length).toBeGreaterThan(0);
  });
  act(() => {
    streams.last.emit({ type: "log_line", line: `${ARCHIVE} (0.1 MiB, verified)` });
    streams.last.emit({ type: "state_change", state: "succeeded", exit_code: 0 });
  });

  const result = await screen.findByText(`${ARCHIVE} (0.1 MiB, verified)`);
  expect(before.some((region) => region.contains(result))).toBe(true);
  // And no region came with it. A region that mounted since (a panel that
  // finished loading) must be empty; one holding the message would be the
  // insertion spec 156 forbids.
  const added = screen.getAllByRole("status").filter((region) => !before.includes(region));
  expect(added.filter((region) => region.textContent !== "")).toEqual([]);
});

test("restore has no button", async () => {
  stubApi(handler({}));
  renderPage();
  await screen.findByRole("heading", { name: "Backups" });
  expect(screen.queryByRole("button", { name: /restore/i })).toBeNull();
});

test("a failed backup says no archive was written", async () => {
  stubApi(handler({ directory: "absent" }));
  renderPage();
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Take a backup" }));
  await waitFor(() => {
    expect(streams.connections.length).toBeGreaterThan(0);
  });
  act(() => {
    streams.last.emit({
      type: "log_line",
      line: "backup failed: archive verification disagreed",
    });
    streams.last.emit({ type: "state_change", state: "failed", exit_code: 1 });
  });
  expect((await screen.findByRole("alert")).textContent).toBe(
    "The backup failed and no archive was written. backup failed: archive verification disagreed",
  );
});

test("a refused backup is shown in the server's words, not as a token refusal", async () => {
  stubApi((call) =>
    call.path === "/api/ops/backup"
      ? {
          status: 409,
          body: { detail: "a backup is already running with other options; wait for it to end" },
        }
      : handler({})(call),
  );
  renderPage();
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Take a backup" }));
  expect((await screen.findByRole("alert")).textContent).toBe(
    "a backup is already running with other options; wait for it to end",
  );
});

test("a backups list refused for its token says so", async () => {
  // The 403 is declared with the API's own sentence (spec 114), and the page
  // shows it in those words.
  const sentence =
    "this request did not present the local API token. " +
    "The UI sends it automatically; a page that is not the harrier UI cannot.";
  stubApi((call) =>
    call.path === "/api/settings/backups"
      ? { status: 403, body: { detail: sentence } }
      : handler({})(call),
  );
  renderPage();
  expect((await screen.findByRole("alert")).textContent).toBe(sentence);
});

test("starting a backup clears a verification's refusal", async () => {
  stubApi((call) =>
    call.path === `/api/settings/backups/${ARCHIVE}/verify`
      ? { status: 404, body: { detail: `no archive named ${ARCHIVE} in the backups list` } }
      : handler({
          archives: [
            {
              name: ARCHIVE,
              size_bytes: 1048576,
              modified_at: "2026-03-01T02:00:00+00:00",
              verification: null,
            },
          ],
        })(call),
  );
  renderPage();
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: `Verify ${ARCHIVE}` }));
  expect((await screen.findByRole("alert")).textContent).toContain("in the backups list");

  await user.click(screen.getByRole("button", { name: "Take a backup" }));
  await waitFor(() => {
    expect(screen.queryByText(/in the backups list/)).toBeNull();
  });
});

test("local settings show no credentials or Telegram section", async () => {
  const calls = stubApi(handler({}));
  renderPage();
  await screen.findByRole("heading", { name: "Backups" });

  expect(screen.queryByRole("heading", { name: "Credentials" })).toBeNull();
  expect(screen.queryByRole("heading", { name: "Telegram notifications" })).toBeNull();
  // Locally both route families answer 404, so neither is asked.
  await new Promise((resolve) => setTimeout(resolve, 30));
  expect(
    calls.filter(
      (call) =>
        call.path.startsWith("/api/credentials") || call.path.startsWith("/api/notifications"),
    ),
  ).toEqual([]);
});

test("settings page shows no schedule section locally", async () => {
  stubApi(handler({}));
  renderPage();
  await screen.findByRole("heading", { name: "Backups" });

  // Locally the schedule is launchd's (spec 120): no section and no form.
  // The host panel still reads the schedule, as it did before.
  expect(screen.queryByRole("heading", { name: "Schedule" })).toBeNull();
  expect(screen.queryByRole("form", { name: "Schedule" })).toBeNull();
});
