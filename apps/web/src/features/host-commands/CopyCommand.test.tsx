import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { CopyCommand } from "./CopyCommand";

/** The copy control announces that it copied (spec 096). */

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

test("copies the exact command and announces it", async () => {
  const user = userEvent.setup();
  render(<CopyCommand command="harrier gmail-oauth" />);
  const status = screen.getByRole("status");
  expect(status.textContent).toBe("");

  await user.click(screen.getByRole("button", { name: "Copy harrier gmail-oauth" }));

  expect(await navigator.clipboard.readText()).toBe("harrier gmail-oauth");
  expect(status.textContent).toBe("Copied");
});

test("a refused clipboard is said, and the command stays on the page", async () => {
  const user = userEvent.setup();
  vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("denied"));
  render(<CopyCommand command="harrier doctor" />);

  await user.click(screen.getByRole("button", { name: "Copy harrier doctor" }));

  expect(screen.getByRole("status").textContent).toBe(
    "Could not copy. Select the command and copy it.",
  );
  expect(screen.getByText("harrier doctor").tagName).toBe("CODE");
});
