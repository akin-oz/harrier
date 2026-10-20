import { expect, test } from "vitest";
import { packageRoot } from "../scripts/options";

test("the package root is a file path, not a URL path", () => {
  // A checkout under a folder with a space in its name.
  const root = packageRoot("file:///tmp/my%20projects/harrier/apps/extension/scripts/options.ts");

  expect(root).toBe("/tmp/my projects/harrier/apps/extension/");
});
