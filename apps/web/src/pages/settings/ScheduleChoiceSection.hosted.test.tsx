import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

// The hosted build: its own file, since the mode is fixed before any module
// is read (spec 114).
vi.hoisted(() => {
  vi.stubEnv("VITE_HARRIER_AUTH", "supabase");
  vi.stubEnv("VITE_SUPABASE_URL", "https://abc.supabase.co");
  vi.stubEnv("VITE_SUPABASE_PUBLISHABLE_KEY", "sb_publishable_synthetic");
  vi.stubEnv("VITE_HARRIER_API_URL", "https://api.example.com");
});

vi.mock("@supabase/supabase-js", async () => ({
  createClient: (await import("../../shared/testing/fakeSupabase")).fake.createClient,
}));

import { SESSION_A, fake } from "../../shared/testing/fakeSupabase";
import { SettingsPage } from "./SettingsPage";

/**
 * The owner's Schedule section in Settings (spec 120). Synthetic values
 * throughout; the API is a stub.
 */

const OFFERED = [
  { name: "discovery", cadence: "daily at 09:00, 13:00, 16:00, 20:00", needs: [] },
  { name: "academic-discovery", cadence: "Mondays at 09:30", needs: ["apify"] },
];

function hostedJob(name: string, next: string) {
  return {
    name,
    cadence: OFFERED.find((job) => job.name === name)?.cadence ?? "",
    records: [],
    hosted: {
      last_slot_at: null,
      last_outcome: null,
      last_started_at: null,
      last_finished_at: null,
      last_ok_at: null,
      next_slot_at: next,
    },
  };
}

function schedule(overrides: Record<string, unknown> = {}) {
  return {
    jobs: [],
    error: null,
    installed_state: "You have not chosen a schedule.",
    host_command: null,
    timezone: null,
    offered: OFFERED,
    ...overrides,
  };
}

type Put = { path: string; body: unknown };

let puts: Put[] = [];
let state: {
  schedule: unknown;
  apifySet: boolean;
  refuse: { status: number; detail: string } | null;
  afterSave: unknown;
};

beforeEach(() => {
  fake.reset();
  fake.signIn(SESSION_A);
  puts = [];
  state = { schedule: schedule(), apifySet: false, refuse: null, afterSave: null };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(String(input));
      const path = new URL(request.url).pathname;
      const reply = (status: number, value: unknown): Response =>
        new Response(JSON.stringify(value), {
          status,
          headers: { "Content-Type": "application/json" },
        });
      if (path === "/config/schedule" && request.method === "PUT") {
        puts.push({ path, body: JSON.parse(await request.text()) as unknown });
        if (state.refuse !== null)
          return reply(state.refuse.status, { detail: state.refuse.detail });
        if (state.afterSave !== null) state.schedule = state.afterSave;
        return reply(200, {
          kind: "schedule",
          value: {},
          source: "store",
          updated_at: null,
          error: null,
        });
      }
      if (path === "/ops/schedule") return reply(200, state.schedule);
      if (path === "/credentials") {
        return reply(200, {
          credentials: [
            { kind: "ai", set: true, updated_at: "2026-10-14 10:00:00" },
            { kind: "apify", set: state.apifySet, updated_at: null },
            { kind: "hunter", set: false, updated_at: null },
            { kind: "telegram", set: false, updated_at: null },
          ],
        });
      }
      // The page's other sections are not under test: they are refused.
      return reply(404, { detail: "not stubbed" });
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderPage(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <SettingsPage />
    </QueryClientProvider>,
  );
}

async function form(): Promise<HTMLElement> {
  return screen.findByRole("form", { name: "Schedule" });
}

test("settings schedule section saves the chosen jobs and zone", async () => {
  state.afterSave = schedule({
    timezone: "Europe/Berlin",
    jobs: [hostedJob("discovery", "2026-10-16T18:00:00Z")],
    installed_state: "Hosted, the scheduler starts each chosen job at its slots in your timezone.",
  });
  renderPage();
  const section = await form();
  const user = userEvent.setup();

  await user.click(within(section).getByRole("checkbox", { name: "discovery" }));
  await user.selectOptions(within(section).getByLabelText("Timezone"), "Europe/Berlin");
  await user.click(within(section).getByRole("button", { name: "Save schedule" }));

  expect(puts).toEqual([
    {
      path: "/config/schedule",
      body: { value: { timezone: "Europe/Berlin", jobs: ["discovery"] } },
    },
  ]);
  const status = await within(section).findByRole("status");
  expect(status.textContent).toContain("Saved. Changes take effect from the next slot.");
  // Read again after the save: 18:00 UTC is 20:00 in Berlin in October.
  expect(await within(status).findByText("discovery: next slot 16 Oct, 20:00")).toBeDefined();
});

test("settings schedule section names an unscheduled and a paused owner", async () => {
  renderPage();
  let section = await form();
  expect(within(section).getByText(/^Not scheduled yet: nothing runs on a schedule/)).toBeDefined();
  // No stored zone: the browser's own is chosen.
  const zone = within(section).getByLabelText<HTMLSelectElement>("Timezone");
  expect(zone.value).toBe(Intl.DateTimeFormat().resolvedOptions().timeZone);
  for (const box of within(section).getAllByRole<HTMLInputElement>("checkbox")) {
    expect(box.checked).toBe(false);
  }
  cleanup();

  state.schedule = schedule({ timezone: "Europe/Berlin", jobs: [] });
  renderPage();
  section = await form();
  expect(within(section).getByText("Paused: no job is chosen.")).toBeDefined();
  expect(within(section).getByLabelText<HTMLSelectElement>("Timezone").value).toBe("Europe/Berlin");
});

test("settings schedule section shows the store's refusal", async () => {
  state.refuse = { status: 400, detail: "schedule.timezone is not a zone this server can load" };
  renderPage();
  const section = await form();
  const user = userEvent.setup();

  await user.click(within(section).getByRole("checkbox", { name: "academic-discovery" }));
  await user.click(within(section).getByRole("button", { name: "Save schedule" }));

  const alert = await within(section).findByRole("alert");
  expect(alert.textContent).toContain("schedule.timezone is not a zone this server can load");
  // The entry is kept, to be corrected rather than retyped.
  expect(
    within(section).getByRole<HTMLInputElement>("checkbox", { name: "academic-discovery" }).checked,
  ).toBe(true);
  expect(within(section).queryByRole("status")).toBeNull();
});

test("settings schedule section notes a job's missing credential", async () => {
  const note = /Needs the Apify credential\. Without it, each slot is recorded as not configured/;
  renderPage();
  let section = await form();
  const academic = (await within(section).findByText(note)).closest(".schedule-form__job");
  expect(academic?.textContent).toContain("academic-discovery");
  // Discovery needs no credential, so only one note shows.
  expect(within(section).getAllByText(note)).toHaveLength(1);
  cleanup();

  state.apifySet = true;
  renderPage();
  section = await form();
  // Once the credentials are read, the saved Apify token removes the note.
  await vi.waitFor(() => {
    expect(within(section).queryByText(note)).toBeNull();
  });
});
