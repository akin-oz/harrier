import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    // Stylesheets load as empty modules in tests. A `?raw` import is the
    // exception, for a test that reads the cascade jsdom does not apply.
    css: { include: [/\.css\?raw$/] },
  },
});
