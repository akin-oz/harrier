import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { stubApi } from "../../shared/testing/stubApi";
import { HostPanel } from "./HostPanel";
import type { CommandPlaces, HostFacts, Schedule } from "./useCommandPlaces";

/**
 * What only the host can do (spec 096): each fact the container can read,
 * beside the command that would change it, and a fact it cannot read shown
 * as not knowable from here, never as healthy. The schedule is read from
 * spec 050's `GET /ops/schedule`, the route the Operations page reads.
 */

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const PLACES: CommandPlaces = {
  routed: [],
  host: [],
  terminal: [],
  panel: [
    { fact: "schedule", commands: ["harrier schedule status", "harrier schedule install"] },
    { fact: "gmail_token", commands: ["harrier gmail-oauth"] },
    { fact: "model", commands: ["harrier scoring train --activate"] },
    { fact: "image", commands: ["just container-up"] },
    { fact: "database_owner", commands: ["harrier doctor", "harrier doctor --integrity"] },
    { fact: "profile", commands: ["harrier profile export --to <directory>"] },
  ],
};

const SCHEDULE: Schedule = {
  jobs: [
    {
      name: "discovery",
      cadence: "daily at 09:00",
      records: [
        {
          key: "discovery",
          last_success_at: "2026-03-03T09:00:00Z",
          summary: "discovery: last succeeded 2 days ago",
          overdue: false,
        },
      ],
    },
    {
      name: "gmail-watch",
      cadence: "every 5 minutes",
      records: [
        {
          key: "mail-watch",
          last_success_at: null,
          summary: "mail-watch: has never recorded a success",
          overdue: true,
        },
      ],
    },
  ],
  error: null,
  installed_state: "Whether each job is installed and loaded is the host's to report.",
  host_command: "harrier schedule status",
};

function facts(overrides: Partial<HostFacts> = {}): HostFacts {
  return {
    gmail_token: { state: "present", age_days: 12 },
    model: { state: "missing", trained_at: null, version: null },
    newest_feature_export: null,
    image_revision: "abc1234",
    database_owner: "unknown",
    ...overrides,
  };
}

function renderPanel(host: HostFacts, schedule: Schedule = SCHEDULE) {
  stubApi((call) => {
    if (call.path === "/api/settings/commands") return { status: 200, body: PLACES };
    if (call.path === "/api/settings/host") return { status: 200, body: host };
    if (call.path === "/api/ops/schedule") return { status: 200, body: schedule };
    return undefined;
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <HostPanel profileCount={3} />
    </QueryClientProvider>,
  );
}

function row(title: string): HTMLElement {
  const heading = screen.getByRole("heading", { name: title });
  const item = heading.closest("li");
  if (item === null) throw new Error(`no row ${title}`);
  return item;
}

test("each fact sits beside the commands that would change it", async () => {
  renderPanel(facts());
  await screen.findByRole("heading", { name: "The schedule" });

  const schedule = row("The schedule");
  expect(within(schedule).getByText("discovery: last succeeded 2 days ago")).toBeDefined();
  expect(
    within(schedule).getByText(/Overdue: mail-watch: has never recorded a success/),
  ).toBeDefined();
  expect(within(schedule).getByText("harrier schedule install")).toBeDefined();
  expect(within(row("The mail token")).getByText(/present, written 12 days ago/)).toBeDefined();
  expect(within(row("The learned score")).getByText(/No trained model is active/)).toBeDefined();
  expect(within(row("The running image")).getByText(/revision abc1234/)).toBeDefined();
  expect(within(row("Profile documents on disk")).getByText(/3 profile documents/)).toBeDefined();
  expect(
    within(row("Profile documents on disk")).getByText("harrier profile export --to <directory>"),
  ).toBeDefined();
});

test("what the container cannot know is said to be unknowable, never shown healthy", async () => {
  renderPanel(facts());
  await screen.findByRole("heading", { name: "Who owns the database" });
  expect(
    within(row("Who owns the database")).getByText("Not knowable from inside the container."),
  ).toBeDefined();
  expect(
    within(row("The schedule")).getByText(/installed and loaded is the host's to report/),
  ).toBeDefined();
  for (const word of ["Healthy", "OK", "Installed", "Loaded"]) {
    expect(screen.queryByText(word)).toBeNull();
  }
});

test("absent facts are shown as absent, each with its command", async () => {
  renderPanel(facts({ gmail_token: { state: "absent", age_days: null } }), {
    ...SCHEDULE,
    jobs: [],
    error: "cannot read config/schedule.json",
  });
  await screen.findByRole("heading", { name: "The schedule" });
  expect(within(row("The schedule")).getByText(/schedule definition cannot be read/)).toBeDefined();
  expect(within(row("The mail token")).getByText("No token file is present.")).toBeDefined();
  expect(within(row("The mail token")).getByText("harrier gmail-oauth")).toBeDefined();
});

test("every command on the panel can be copied", async () => {
  renderPanel(facts());
  await screen.findByRole("heading", { name: "The schedule" });
  const user = userEvent.setup();
  for (const { commands } of PLACES.panel) {
    for (const command of commands) {
      await user.click(screen.getByRole("button", { name: `Copy ${command}` }));
      expect(await navigator.clipboard.readText()).toBe(command);
    }
  }
});
