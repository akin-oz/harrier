import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { CommandList } from "../features/host-commands/CommandList";
import { AddTrack } from "../features/tracks/AddTrack";
import { ScheduleSection } from "../pages/operations/ScheduleSection";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

// Hosted, a read the API cannot serve while it has no signing keys says why
// (spec 104): the page shows that sentence, not a generic one that hides it.
const DETAIL = "The signing keys cannot be fetched now. Try again shortly.";

function refuse(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify({ reason: "keys_unavailable", detail: DETAIL }), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    ),
  );
}

function page(children: React.ReactNode): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}>{children}</QueryClientProvider>);
}

test("a read refused for unavailable keys shows the API's sentence", async () => {
  for (const [name, element, text] of [
    ["the schedule", <ScheduleSection key="schedule" />, DETAIL],
    ["the command list", <CommandList key="commands" />, DETAIL],
    ["the track kinds", <AddTrack key="kinds" onOpen={() => undefined} />, DETAIL],
  ] as const) {
    refuse();
    page(element);
    expect((await screen.findByText(new RegExp(text))).textContent, name).toContain(text);
    cleanup();
  }
});
