import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

// A bundle built with a bad hosted configuration anyway: the switch is on and
// the Supabase URL is missing (spec 114).
vi.hoisted(() => {
  vi.stubEnv("VITE_HARRIER_AUTH", "supabase");
  vi.stubEnv("VITE_SUPABASE_PUBLISHABLE_KEY", "sb_publishable_synthetic");
  vi.stubEnv("VITE_HARRIER_API_URL", "https://api.example.com");
});

vi.mock("@supabase/supabase-js", async () => ({
  createClient: (await import("../shared/testing/fakeSupabase")).fake.createClient,
}));

import { fake } from "../shared/testing/fakeSupabase";
import { App } from "./App";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("a misconfigured build says so and calls nothing", async () => {
  const asked: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      asked.push(String(input instanceof Request ? input.url : input));
      return Promise.resolve(new Response("{}"));
    }),
  );
  render(<App />);

  const notice = await screen.findByRole("alert");
  expect(notice.textContent).toContain("This build of harrier is misconfigured:");
  expect(notice.textContent).toContain("VITE_SUPABASE_URL");
  // Nothing else renders, and nothing is asked of anyone.
  expect(screen.queryByRole("navigation", { name: "Sections" })).toBeNull();
  expect(screen.queryByLabelText("Email address")).toBeNull();
  await new Promise((resolve) => setTimeout(resolve, 30));
  expect(asked).toEqual([]);
  expect(fake.createClient).not.toHaveBeenCalled();
});
