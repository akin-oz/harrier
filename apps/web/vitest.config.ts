import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // A developer's apps/web/.env files must not decide which mode a test runs in:
  // each hosted test file sets the build's variables itself (spec 114).
  envDir: false,
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    // Stylesheets load as empty modules in tests. A `?raw` import is the
    // exception, for a test that reads the cascade jsdom does not apply.
    css: { include: [/\.css\?raw$/] },
  },
});
