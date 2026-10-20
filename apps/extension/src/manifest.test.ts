import { expect, test } from "vitest";
import { DEFAULT_TITLE } from "./capture";
import manifest from "./manifest.json";

test("the manifest asks for four permissions and no host access", () => {
  expect(manifest.manifest_version).toBe(3);
  expect([...manifest.permissions].sort()).toEqual([
    "activeTab",
    "contextMenus",
    "scripting",
    "storage",
  ]);
  const keys = Object.keys(manifest);
  for (const absent of ["host_permissions", "optional_host_permissions", "content_scripts"]) {
    expect(keys).not.toContain(absent);
  }
  expect(manifest.action.default_title).toBe(DEFAULT_TITLE);
});
