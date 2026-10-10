import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { Job } from "../../entities/job";
import { ACADEMIC_TRACK, INDUSTRY_TRACK } from "../../shared/track/fixtures";
import type { Track } from "../../shared/track";
import { JobHistory } from "./JobHistory";

/**
 * A row's decision history (spec 095): read on demand, in order, in the
 * track's own words, with a reconstructed event marked as such.
 */

beforeEach(() => {
  window.history.replaceState(null, "", "/");
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

// An invented company (ADR-008).
const JOB: Job = {
  id: 7,
  company: "Northwind Labs",
  title: "Research Engineer",
  location: "Remote, Europe",
  url: "",
  source: "manual",
  added_at: "2026-03-02",
  fit_score: "",
  status: "rejected",
  applied_date: "",
  last_contact: "",
  next_action: "",
  outreach_status: "",
  last_outreach_at: "",
  next_outreach_action: "",
  best_contact_name: "",
  best_contact_linkedin: "",
  contacts_found: "",
  outreach_priority: "",
  rejection_reason: "",
  notes: "",
  score: "",
  archetype: "",
  source_label: "",
  external_key: "",
  signals: "",
  remote_filter: "",
  manual_added: "",
  created_at: "",
  updated_at: "",
  track: "second-search",
  deadline: "",
  deadline_passed: false,
};

function event(overrides: Record<string, unknown>) {
  return {
    at: "2026-03-02T09:00:00Z",
    kind: "decision",
    actor: "candidate",
    from_status: "",
    to_status: "prospect",
    reason_code: "",
    reason_label: "",
    reason_text: "",
    fit_score: "",
    backfilled: false,
    ...overrides,
  };
}

function stubEvents(body: unknown, status = 200): { url: string; token: string | null }[] {
  const asked: { url: string; token: string | null }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(String(input), init);
      const url = new URL(request.url);
      asked.push({ url: url.pathname + url.search, token: request.headers.get("X-Harrier-Token") });
      return Promise.resolve(
        new Response(JSON.stringify(body), {
          status,
          headers: { "Content-Type": "application/json" },
        }),
      );
    }),
  );
  return asked;
}

function renderHistory(track: Track): void {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <JobHistory job={JOB} track={track} />
    </QueryClientProvider>,
  );
}

test("the history is read on demand, in order, in the track's own words", async () => {
  window.history.replaceState(null, "", "/?track=second-search");
  const asked = stubEvents([
    event({ kind: "created", actor: "system" }),
    event({
      at: "2026-03-03T10:00:00Z",
      from_status: "prospect",
      to_status: "applied",
      fit_score: "71",
    }),
    event({
      at: "2026-03-09T00:00:00Z",
      kind: "outcome",
      actor: "company",
      from_status: "applied",
      to_status: "rejected",
      reason_code: "company_rejected",
      reason_label: "rejected by company",
      reason_text: "an invented note",
      backfilled: true,
    }),
  ]);
  const user = userEvent.setup();
  renderHistory(ACADEMIC_TRACK);

  // Nothing is fetched until the operator asks.
  expect(asked).toHaveLength(0);
  await user.click(screen.getByRole("button", { name: "History" }));
  const list = await screen.findByRole("list", {
    name: "History of Northwind Labs, Research Engineer",
  });
  const lines = within(list).getAllByRole("listitem");
  expect(lines.map((line) => line.textContent)).toEqual([
    expect.stringContaining("Found"),
    expect.stringContaining("Found → Submitted"),
    expect.stringContaining("Submitted → Closed"),
  ]);
  expect(lines[1]?.textContent).toContain("score 71");
  expect(lines[2]?.textContent).toContain("rejected by company");
  expect(lines[2]?.textContent).toContain("an invented note");
  expect(lines[2]?.textContent).toContain("reconstructed");
  expect(lines[1]?.textContent).not.toContain("reconstructed");
  // On the selected track, and a read without the token (spec 095).
  expect(asked[0]?.url).toBe("/api/tracker/7/events?track=second-search");
  expect(asked[0]?.token).toBeNull();
});

test("a refused history is shown in the API's words", async () => {
  stubEvents({ detail: "no job matches 7" }, 404);
  const user = userEvent.setup();
  renderHistory(INDUSTRY_TRACK);

  await user.click(screen.getByRole("button", { name: "History" }));
  expect((await screen.findByRole("alert")).textContent).toBe("no job matches 7");
});

test("a job with no recorded events says so", async () => {
  stubEvents([]);
  const user = userEvent.setup();
  renderHistory(INDUSTRY_TRACK);

  await user.click(screen.getByRole("button", { name: "History" }));
  expect(await screen.findByText("No events are recorded for this job.")).toBeTruthy();
});
