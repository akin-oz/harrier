import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // A developer's apps/web/.env files must not decide which mode a test runs in:
  // each hosted test file sets the build's variables itself (spec 114).
  envDir: false,
  test: {
    // jsdom with Node's AbortController and AbortSignal (vitest.jsdom.ts).
    environment: "./vitest.jsdom.ts",
    include: ["src/**/*.test.{ts,tsx}"],
    // Stylesheets load as empty modules in tests. A `?raw` import is the
    // exception, for a test that reads the cascade jsdom does not apply.
    css: { include: [/\.css\?raw$/] },
    // Node 25 and later ship their own `localStorage` and `sessionStorage`,
    // which read as undefined without `--localstorage-file` and hide
    // jsdom's. CI runs Node 22, which has neither; a newer local Node must
    // not fail the same tests.
    poolOptions: {
      forks: { execArgv: ["--no-experimental-webstorage"] },
    },
  },
});
