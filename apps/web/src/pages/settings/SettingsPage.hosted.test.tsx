import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
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

let paths: string[] = [];

beforeEach(() => {
  fake.reset();
  fake.signIn(SESSION_A);
  paths = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(String(input));
      const path = new URL(request.url).pathname;
      paths.push(path);
      const body = (value: unknown): Promise<Response> =>
        Promise.resolve(
          new Response(JSON.stringify(value), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      if (path === "/credentials") {
        return body({
          credentials: [
            { kind: "ai", set: true, updated_at: "2026-10-14 10:00:00" },
            { kind: "apify", set: false, updated_at: null },
            { kind: "hunter", set: false, updated_at: null },
            { kind: "telegram", set: true, updated_at: "2026-10-13 09:00:00" },
          ],
        });
      }
      if (path === "/notifications/telegram") {
        return body({ configured: true, updated_at: "2026-10-13 09:00:00" });
      }
      // The page's other sections are not under test: they are refused.
      return Promise.resolve(
        new Response(JSON.stringify({ detail: "not stubbed" }), {
          status: 404,
          headers: { "Content-Type": "application/json" },
        }),
      );
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("the credentials page shows each kind's state and never a value", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <SettingsPage />
    </QueryClientProvider>,
  );

  const ai = await screen.findByRole("form", { name: "AI provider key" });
  expect(within(ai).getByText("Set, updated 2026-10-14 10:00:00")).toBeDefined();
  expect(
    within(screen.getByRole("form", { name: "Apify token" })).getByText("Not set"),
  ).toBeDefined();
  expect(
    within(screen.getByRole("form", { name: "Hunter key" })).getByText("Not set"),
  ).toBeDefined();
  // Telegram's row only points to its own form: it has no fields here.
  expect(screen.getByText(/^Telegram: Set, updated 2026-10-13 09:00:00/)).toBeDefined();
  expect(screen.queryByRole("form", { name: "Telegram key" })).toBeNull();
  const telegram = await screen.findByRole("form", { name: "Telegram notifications" });
  expect(within(telegram).getByText("Configured, updated 2026-10-13 09:00:00")).toBeDefined();

  // No field holds a value the API sent: it sends none.
  for (const input of screen.getAllByLabelText<HTMLInputElement>(
    /^(Key|Token|Bot token|Chat id)$/,
  )) {
    expect(input.value).toBe("");
    expect(input.type).toBe("password");
  }
  expect(paths).toContain("/credentials");
  expect(paths).toContain("/notifications/telegram");
});
