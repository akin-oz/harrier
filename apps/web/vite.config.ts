import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

import { readAppMode } from "./src/shared/config/mode";

// Where Vite reads `.env` files from: this directory, whatever the shell's
// working directory is.
const ENV_DIR = decodeURIComponent(new URL(".", import.meta.url).pathname);

export default defineConfig(({ mode }) => {
  // A bad hosted configuration stops `pnpm dev` and `pnpm build` here, naming
  // the variable, before anything is bundled (spec 114). Variables already in
  // the process environment win over `.env` files.
  readAppMode(loadEnv(mode, ENV_DIR, "VITE_"));
  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        "/api": {
          target: "http://127.0.0.1:8000",
          rewrite: (path: string) => path.replace(/^\/api/, ""),
        },
      },
    },
  };
});
