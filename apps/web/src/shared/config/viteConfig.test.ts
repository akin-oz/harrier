// @vitest-environment node
import { afterEach, expect, test, vi } from "vitest";

import config from "../../../vite.config";

afterEach(() => {
  vi.unstubAllEnvs();
});

// What `vite build` and `vite dev` hand the config function.
const BUILD = { command: "build", mode: "production" } as const;

// `loadEnv` reads the developer's own `.env` files in this directory as well as
// the process environment, and the process environment wins. Each test states
// all four variables, so no file can change what it proves.
test("a hosted build with no other hosted variable stops before it bundles", () => {
  vi.stubEnv("VITE_HARRIER_AUTH", "supabase");
  vi.stubEnv("VITE_SUPABASE_URL", "");
  vi.stubEnv("VITE_SUPABASE_PUBLISHABLE_KEY", "");
  vi.stubEnv("VITE_HARRIER_API_URL", "");
  expect(() => config(BUILD)).toThrow("VITE_SUPABASE_URL is required");
});

test("a hosted build with every variable, and a local build, are configured", () => {
  vi.stubEnv("VITE_HARRIER_AUTH", "supabase");
  vi.stubEnv("VITE_SUPABASE_URL", "https://abc.supabase.co");
  vi.stubEnv("VITE_SUPABASE_PUBLISHABLE_KEY", "sb_publishable_synthetic");
  vi.stubEnv("VITE_HARRIER_API_URL", "https://api.example.com");
  expect(config(BUILD)).toHaveProperty("plugins");

  vi.stubEnv("VITE_HARRIER_AUTH", "");
  expect(config(BUILD)).toHaveProperty("plugins");
});
