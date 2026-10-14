// @vitest-environment node
import { afterEach, expect, test, vi } from "vitest";

import config from "../../../vite.config";

afterEach(() => {
  vi.unstubAllEnvs();
});

// What `vite build` and `vite dev` hand the config function.
const BUILD = { command: "build", mode: "production" } as const;

test("a hosted build with no other hosted variable stops before it bundles", () => {
  vi.stubEnv("VITE_HARRIER_AUTH", "supabase");
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
