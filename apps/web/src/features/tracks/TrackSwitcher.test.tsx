import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { ACADEMIC_TRACK, INDUSTRY_TRACK } from "../../shared/track/fixtures";
import { TrackSwitcher } from "./TrackSwitcher";

/**
 * The switcher works from the keyboard and says its state (spec 094): the
 * button names the track and its kind, the menu checks the selected one, and
 * archived tracks are on the tracks page rather than here.
 */

beforeEach(() => {
  window.history.replaceState(null, "", "/");
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve(
        new Response(
          JSON.stringify([
            INDUSTRY_TRACK,
            ACADEMIC_TRACK,
            { ...ACADEMIC_TRACK, id: 3, slug: "old-search", label: "Old search", archived: true },
          ]),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    ),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

function renderSwitcher(onManage: () => void = () => undefined): void {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <TrackSwitcher onManage={onManage} />
    </QueryClientProvider>,
  );
}

test("the switcher is driven from the keyboard and announces its state", async () => {
  const user = userEvent.setup();
  renderSwitcher();

  const button = await screen.findByRole("button", { name: /Job search/ });
  expect(button.textContent).toContain("industry");
  expect(button.getAttribute("aria-haspopup")).toBe("menu");
  expect(button.getAttribute("aria-expanded")).toBe("false");

  button.focus();
  await user.keyboard("{ArrowDown}");
  expect(button.getAttribute("aria-expanded")).toBe("true");
  const items = screen.getAllByRole("menuitemradio");
  // Live tracks only; the archived one is on the tracks page.
  expect(items.map((item) => item.getAttribute("aria-label"))).toEqual([
    "Job search, industry",
    "Second search, academic",
  ]);
  expect(items.map((item) => item.getAttribute("aria-checked"))).toEqual(["true", "false"]);
  // Focus starts on the selected track.
  expect(document.activeElement).toBe(items[0]);

  await user.keyboard("{ArrowDown}");
  expect(document.activeElement).toBe(items[1]);
  await user.keyboard("{End}");
  expect(document.activeElement).toBe(screen.getByRole("menuitem", { name: "Manage tracks" }));
  await user.keyboard("{Home}{ArrowDown}{Enter}");

  expect(window.location.search).toBe("?track=second-search");
  expect(screen.queryByRole("menu")).toBeNull();
  await waitFor(() => {
    expect(document.activeElement?.textContent).toContain("Second search");
  });
  expect(document.activeElement?.textContent).toContain("academic");
});

test("Escape closes the menu without switching, and focus returns to the button", async () => {
  const user = userEvent.setup();
  renderSwitcher();

  const button = await screen.findByRole("button", { name: /Job search/ });
  await user.click(button);
  expect(screen.getByRole("menu", { name: "Search tracks" })).toBeDefined();
  await user.keyboard("{ArrowDown}{Escape}");
  expect(screen.queryByRole("menu")).toBeNull();
  expect(document.activeElement).toBe(button);
  expect(window.location.search).toBe("");
});

test("Manage tracks leads to the tracks page", async () => {
  const user = userEvent.setup();
  const managed: string[] = [];
  renderSwitcher(() => managed.push("tracks"));

  await user.click(await screen.findByRole("button", { name: /Job search/ }));
  await user.click(screen.getByRole("menuitem", { name: "Manage tracks" }));
  expect(managed).toEqual(["tracks"]);
});
