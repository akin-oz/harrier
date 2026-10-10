import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { ACADEMIC_TRACK, INDUSTRY_TRACK } from "../shared/track/fixtures";
import { App, queryClient } from "./App";

/**
 * The shell follows the track the URL names (spec 094): it keys every cache
 * entry of track data by that track, shows sections the track cannot use as
 * unavailable with the reason, and says plainly when the URL names a track
 * that does not exist.
 */

const ROW = {
  id: 1,
  company: "Example Co",
  title: "Senior Frontend Engineer",
  url: "https://boards.example.com/x/1",
  location: "Remote, Europe",
  source: "greenhouse",
  status: "prospect",
  score: "80",
  fit_score: "",
  next_action: "",
  added_at: "2026-01-01",
  track: "job",
  deadline: "",
  deadline_passed: false,
};

function stubApi(): string[] {
  const asked: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(String(input), init);
      const url = new URL(request.url);
      asked.push(url.pathname + url.search);
      const reply = (body: unknown, status = 200): Promise<Response> =>
        Promise.resolve(
          new Response(JSON.stringify(body), {
            status,
            headers: { "Content-Type": "application/json" },
          }),
        );
      if (url.pathname === "/api/tracks") return reply([INDUSTRY_TRACK, ACADEMIC_TRACK]);
      if (url.pathname === "/api/tracks/kinds") return reply([]);
      if (url.pathname === "/api/session") return reply({ token: "test-token" });
      if (url.pathname === "/api/jobs") return reply([ROW]);
      if (url.pathname === "/api/health") return reply({ status: "ok" });
      if (url.pathname === "/api/apply/1/artifacts") return reply([]);
      if (url.pathname === "/api/settings/commands") {
        return reply({ routed: [], host: [], terminal: [], panel: [] });
      }
      if (url.pathname === "/api/ops/schedule") {
        return reply({ jobs: [], error: null, installed_state: "", host_command: "" });
      }
      if (url.pathname === "/api/settings/backups") {
        return reply({ directory: "absent", archives: [] });
      }
      if (url.pathname === "/api/settings/host") {
        return reply({
          gmail_token: { state: "not_configured", age_days: null },
          model: { state: "missing", trained_at: null, version: null },
          newest_feature_export: null,
          image_revision: "unknown",
          database_owner: "unknown",
        });
      }
      return reply([]);
    }),
  );
  return asked;
}

beforeEach(() => {
  window.history.replaceState(null, "", "/");
});

afterEach(() => {
  cleanup();
  queryClient.clear();
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

test("renders the shell and the tracker fed by /jobs", async () => {
  stubApi();
  render(<App />);
  expect(screen.getByRole("heading", { name: "Harrier" })).toBeDefined();
  await waitFor(() => {
    expect(screen.getByText("Example Co")).toBeDefined();
  });
  expect(screen.getByRole("heading", { name: /Job search/ })).toBeDefined();
});

test("sections the track cannot use are shown unavailable, with the reason", async () => {
  const asked = stubApi();
  window.history.replaceState(null, "", "/?track=second-search");
  const user = userEvent.setup();
  render(<App />);

  const outreach = await screen.findByRole("button", { name: "Outreach" });
  await waitFor(() => {
    expect(outreach.getAttribute("aria-disabled")).toBe("true");
  });
  expect(screen.getByRole("button", { name: "Inbox" }).getAttribute("aria-disabled")).toBe("true");
  expect(screen.getByRole("button", { name: "Tracker" }).getAttribute("aria-disabled")).toBeNull();

  await user.click(outreach);
  expect(
    screen.getByText(
      "Not available on Second search: it uses the default track's profile and configuration.",
    ),
  ).toBeDefined();
  await user.click(screen.getByRole("button", { name: "Inbox" }));
  expect(screen.getByRole("heading", { name: "Inbox" })).toBeDefined();
  expect(screen.getByText(/^Not available on Second search/)).toBeDefined();
  // Shown unavailable, and asked nothing: the routes would refuse it.
  expect(asked.some((path) => path.startsWith("/api/outreach"))).toBe(false);
  expect(asked.some((path) => path.startsWith("/api/mail"))).toBe(false);

  // On the default track the same sections are live.
  await user.click(screen.getByRole("link", { name: "Switch to the default track" }));
  await waitFor(() => {
    expect(screen.getByRole("button", { name: "Inbox" }).getAttribute("aria-disabled")).toBeNull();
  });
  expect(window.location.search).toBe("");
});

test("an unknown track shows a message and a link back to the default track", async () => {
  const asked = stubApi();
  window.history.replaceState(null, "", "/?track=no-such-track");
  const user = userEvent.setup();
  render(<App />);

  expect(
    await screen.findByRole("heading", { name: "There is no track named “no-such-track”" }),
  ).toBeDefined();
  // A message, not an empty table that looks like a track with no rows.
  expect(screen.queryByRole("table")).toBeNull();
  expect(asked.some((path) => path.startsWith("/api/jobs"))).toBe(false);

  const back = screen.getByRole("link", { name: "Go to Job search" });
  expect(back.getAttribute("href")).toBe("/");
  await user.click(back);
  await waitFor(() => {
    expect(screen.getByText("Example Co")).toBeDefined();
  });
  expect(window.location.search).toBe("");
});

test("every cache entry of track data is keyed by the selected track", async () => {
  stubApi();
  window.history.replaceState(null, "", "/?track=job");
  const user = userEvent.setup();
  render(<App />);

  // Visit every section that reads track data on the default track, the
  // Apply view included, so each of their hooks fills the cache.
  await screen.findByText("Example Co");
  await user.click(screen.getByRole("button", { name: "Outreach" }));
  await user.click(screen.getByRole("button", { name: "Inbox" }));
  await user.click(screen.getByRole("button", { name: "Tracker" }));
  await user.click(await screen.findByRole("button", { name: /^Apply to Example Co/ }));
  await screen.findByRole("heading", { name: "Example Co" });

  // The same answer on every track: the list of tracks, the kinds, the
  // service's health and a run by its id. Everything else is a track's.
  const global = new Set(["tracks", "track-kinds", "health", "run"]);
  const keyed = (slug: string): void => {
    const keys = queryClient
      .getQueryCache()
      .getAll()
      .map((query) => query.queryKey)
      .filter((key) => !global.has(String(key[0])));
    expect(keys.length).toBeGreaterThan(0);
    for (const key of keys) expect(key[0], JSON.stringify(key)).toBe(slug);
  };
  keyed("job");
  const before = new Set(["artifacts", "outreach", "mail", "jobs"]);
  const seen = new Set(
    queryClient
      .getQueryCache()
      .getAll()
      .map((query) => String(query.queryKey[1])),
  );
  for (const family of before) expect(seen.has(family), family).toBe(true);

  // A switch reads under the new track's key, never the old one's.
  queryClient.clear();
  window.history.pushState(null, "", "/?track=second-search");
  window.dispatchEvent(new PopStateEvent("popstate"));
  await screen.findByRole("heading", { name: /Second search/ });
  await waitFor(() => {
    keyed("second-search");
  });
});

test("Settings is the last section, and the same on every track", async () => {
  const asked = stubApi();
  window.history.replaceState(null, "", "/?track=second-search");
  const user = userEvent.setup();
  render(<App />);

  const nav = screen.getByRole("navigation", { name: "Sections" });
  const sections = within(nav)
    .getAllByRole("button")
    .map((button) => button.textContent);
  expect(sections.at(-1)).toBe("Settings");

  const settings = within(nav).getByRole("button", { name: "Settings" });
  expect(settings.getAttribute("aria-disabled")).toBeNull();
  await user.click(settings);
  await screen.findByRole("heading", { name: "Settings", level: 2 });
  await waitFor(() => {
    expect(asked.some((path) => path.startsWith("/api/settings/host"))).toBe(true);
  });
  // Install-wide reads name no track.
  const reads = asked.filter((path) => path.startsWith("/api/settings"));
  expect(reads.every((path) => !path.includes("track="))).toBe(true);
});
