import type { BuildOptions } from "esbuild";

const root = new URL("..", import.meta.url).pathname;

// The reader is injected with `chrome.scripting.executeScript({ files })`,
// which hands back the value of the script's last statement. The bundle
// assigns the module to a global, and the footer names its fields last.
export const READER: BuildOptions = {
  entryPoints: [`${root}src/reader/inject.ts`],
  bundle: true,
  format: "iife",
  globalName: "harrierReader",
  footer: { js: "harrierReader.fields;" },
  target: "chrome120",
};

// The service worker and the options page are modules.
export const MODULES: BuildOptions = {
  entryPoints: {
    background: `${root}src/background.ts`,
    options: `${root}src/options/main.ts`,
  },
  bundle: true,
  format: "esm",
  target: "chrome120",
};

export const ROOT = root;
