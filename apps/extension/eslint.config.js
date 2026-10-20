import js from "@eslint/js";
import tseslint from "typescript-eslint";

// The extension makes no network request (spec 161): it reads the page the
// owner clicked on and hands what it read to the web app in a new tab's
// address. These are the browser's ways to send a request from script.
const NETWORK = ["fetch", "XMLHttpRequest", "WebSocket"].map((name) => ({
  name,
  message: "The extension makes no network request (spec 161).",
}));

export default tseslint.config(
  { ignores: ["dist"] },
  js.configs.recommended,
  ...tseslint.configs.strictTypeChecked,
  {
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
  },
  {
    files: ["src/**/*.ts"],
    rules: {
      "no-restricted-globals": ["error", ...NETWORK],
      // The same calls by their other names.
      "no-restricted-properties": [
        "error",
        ...["window", "globalThis", "self"].flatMap((object) =>
          NETWORK.map(({ name, message }) => ({ object, property: name, message })),
        ),
      ],
    },
  },
);
