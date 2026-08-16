import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { OutreachPage } from "./OutreachPage";

/**
 * The Outreach page, and the two invariants a UI erodes quietly (spec 048).
 *
 * Approving is what creates a contact, and the page has to say so rather
 * than implying that finding contacts already did. Nothing sends, and the
 * page has to say that too, because a control beside a generated draft is
 * exactly where a reader assumes otherwise.
 */

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

type Call = { url: string; method: string; body: unknown; token: string | null };

// An invented person at an invented company (ADR-008).
const CANDIDATE_URL = "https://www.linkedin.com/in/invented-person-nw";

function candidate(reviewStatus = "pending") {
  return {
    person_name: "Avery Invented",
    person_title: "Engineering Manager",
    relevance: "hiring_manager",
    fit_score: "82",
    linkedin_url: CANDIDATE_URL,
    review_status: reviewStatus,
  };
}

function dueRow() {
  return {
    id: 12,
    company: "Northwind Labs",
    title: "Senior Frontend Engineer",
    outreach_status: "ready",
    next_outreach_action: "send first message",
    best_contact_name: "Avery Invented",
    best_contact_linkedin: CANDIDATE_URL,
  };
}

function stubApi(options: {
  due?: unknown[];
  contacts?: unknown[];
  candidates?: unknown[];
  approve?: { code: number; body: unknown };
  mark?: { code: number; body: unknown };
}): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(String(input), init);
      const url = new URL(request.url);
      const record = async (): Promise<void> => {
        const raw = request.method === "GET" ? "" : await request.clone().text();
        calls.push({
          url: url.pathname,
          method: request.method,
          body: raw === "" ? null : JSON.parse(raw),
          token: request.headers.get("X-Harrier-Token"),
        });
      };
      const reply = (code: number, body: unknown): Promise<Response> =>
        record().then(
          () =>
            new Response(JSON.stringify(body), {
              status: code,
              headers: { "Content-Type": "application/json" },
            }),
        );

      if (url.pathname === "/api/session") return reply(200, { token: "test-token" });
      if (url.pathname === "/api/outreach/due") return reply(200, options.due ?? []);
      if (url.pathname === "/api/outreach/contacts") return reply(200, options.contacts ?? []);
      if (url.pathname.endsWith("/candidates")) return reply(200, options.candidates ?? []);
      if (url.pathname.endsWith("/approve")) {
        const answer = options.approve ?? { code: 200, body: {} };
        return reply(answer.code, answer.body);
      }
      if (url.pathname.endsWith("/sent") || url.pathname.endsWith("/replied")) {
        const answer = options.mark ?? { code: 200, body: dueRow() };
        return reply(answer.code, answer.body);
      }
      return reply(200, {});
    }),
  );
  return calls;
}

function renderPage(): void {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <OutreachPage />
    </QueryClientProvider>,
  );
}

// --- nothing sends -----------------------------------------------------------

test("the page says plainly that nothing sends", async () => {
  stubApi({});
  renderPage();

  const note = await screen.findByText(/Nothing here sends anything/);
  expect(note.textContent).toContain("records that you already did");
  // The invariant would be betrayed by a control named this way.
  expect(screen.queryByRole("button", { name: /^Send/ })).toBeNull();
});

test("marking sent records rather than sends", async () => {
  const calls = stubApi({ due: [dueRow()] });
  const user = userEvent.setup();
  renderPage();

  const row = (await screen.findByText("Northwind Labs")).closest("tr");
  expect(row).not.toBeNull();
  await user.click(within(row as HTMLElement).getByRole("button", { name: "Mark sent" }));

  await waitFor(() => {
    expect(calls.some((call) => call.url === "/api/outreach/12/sent")).toBe(true);
  });
});

// --- staging -----------------------------------------------------------------

test("the page says approving is what creates a contact", async () => {
  stubApi({ candidates: [candidate()] });
  const user = userEvent.setup();
  renderPage();

  await user.type(screen.getByLabelText("Job id"), "12");
  const note = await screen.findByText(/Approving is what creates a contact/);
  expect(note.textContent).toContain("nothing else does");
});

test("approving posts to the approval route with the candidate's identifier", async () => {
  const calls = stubApi({ candidates: [candidate()] });
  const user = userEvent.setup();
  renderPage();

  await user.type(screen.getByLabelText("Job id"), "12");
  await user.click(await screen.findByRole("button", { name: "Approve" }));

  await waitFor(() => {
    expect(calls.some((call) => call.url === "/api/outreach/12/candidates/approve")).toBe(true);
  });
  const sent = calls.find((call) => call.url === "/api/outreach/12/candidates/approve");
  expect(sent?.body).toEqual({ linkedin_url: CANDIDATE_URL });
});

test("a refused approval is shown in the words the API used", async () => {
  stubApi({
    candidates: [candidate()],
    approve: { code: 404, body: { detail: "candidate not found in the staged artifact" } },
  });
  const user = userEvent.setup();
  renderPage();

  await user.type(screen.getByLabelText("Job id"), "12");
  await user.click(await screen.findByRole("button", { name: "Approve" }));

  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toContain("candidate not found in the staged artifact");
});

test("no candidates reads as none found rather than an empty table", async () => {
  stubApi({ candidates: [] });
  const user = userEvent.setup();
  renderPage();

  await user.type(screen.getByLabelText("Job id"), "12");
  expect(await screen.findByText("No candidates found for this job.")).toBeTruthy();
});

// --- money -------------------------------------------------------------------

test("contact discovery is marked as reaching a paid service", async () => {
  stubApi({});
  renderPage();

  const marker = await screen.findByText(/calls a paid service/);
  expect(marker.textContent).toContain("Hunter");
});

// --- the token ---------------------------------------------------------------

test("outreach reads carry the token", async () => {
  const calls = stubApi({ due: [dueRow()] });
  renderPage();

  await waitFor(() => {
    expect(calls.some((call) => call.url === "/api/outreach/due")).toBe(true);
  });
  // These name a real human being who never chose to use this tool, which is
  // why they authenticate although tracker reads do not (spec 048).
  const read = calls.find((call) => call.url === "/api/outreach/due");
  expect(read?.method).toBe("GET");
  expect(read?.token).toBe("test-token");
});
