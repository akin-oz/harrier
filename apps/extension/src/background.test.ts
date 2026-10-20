import { expect, test, vi } from "vitest";
import { MENU_ID } from "./capture";

test("the context menu item appears only on a selection", async () => {
  const created: unknown[] = [];
  let installed: (() => void) | undefined;
  const listener = () => ({ addListener: vi.fn() });
  vi.stubGlobal("chrome", {
    runtime: {
      onInstalled: {
        addListener: (callback: () => void) => {
          installed = callback;
        },
      },
    },
    contextMenus: {
      create: (properties: unknown) => created.push(properties),
      onClicked: listener(),
    },
    action: { onClicked: listener() },
  });

  await import("./background");
  installed?.();

  expect(created).toEqual([
    { id: MENU_ID, title: "Add selection to Harrier", contexts: ["selection"] },
  ]);
  vi.unstubAllGlobals();
});
