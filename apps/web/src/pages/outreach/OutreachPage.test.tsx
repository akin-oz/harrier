import type { components, operations } from "@harrier/contract";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, expectTypeOf, test, vi } from "vitest";

import { OutreachPage, refused } from "./OutreachPage";

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
  // A refusal for the route whose path ends this way, answered before any
  // other stub, so one call can be refused while the rest of the page loads.
  refuse?: { ending: string; code: number; body: unknown };
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
      if (options.refuse !== undefined && url.pathname.endsWith(options.refuse.ending)) {
        return reply(options.refuse.code, options.refuse.body);
      }
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

test("a decision cannot land on a job the operator has navigated away from", async () => {
  // The debounce introduced this: the rows on screen belonged to the settled
  // value while a decision sent the raw one, so approving during that window
  // applied a decision about one job to another (review finding on PR #51).
  const calls = stubApi({ candidates: [candidate()] });
  const user = userEvent.setup();
  renderPage();

  await user.type(screen.getByLabelText("Job id"), "12");
  await screen.findByRole("button", { name: "Approve" });

  // Type on, so the typed value and the settled one disagree.
  await user.type(screen.getByLabelText("Job id"), "9");
  expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();

  await waitFor(() => {
    expect(screen.getByRole("button", { name: "Approve" })).toBeTruthy();
  });
  await user.click(screen.getByRole("button", { name: "Approve" }));

  await waitFor(() => {
    expect(calls.some((call) => call.url.endsWith("/candidates/approve"))).toBe(true);
  });
  const sent = calls.find((call) => call.url.endsWith("/candidates/approve"));
  // 129, the job whose candidates are on screen, never 12.
  expect(sent?.url).toBe("/api/outreach/129/candidates/approve");
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

// --- refusals in the API's words --------------------------------------------

type Act = (user: ReturnType<typeof userEvent.setup>) => Promise<void>;

async function clickInDueRow(user: ReturnType<typeof userEvent.setup>, name: string) {
  const row = (await screen.findByText("Northwind Labs")).closest("tr");
  expect(row).not.toBeNull();
  await user.click(within(row as HTMLElement).getByRole("button", { name }));
}

async function openJob(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("Job id"), "12");
}

const HELD = {
  detail: "A host process holds the tracker database. This request is refused until it ends.",
  hold: { subcommand: "backup", since: "2026-10-11T09:00:00Z" },
};

// Every call site but Approve, which has its own test above. Each message is
// invented, so seeing it proves the page showed the body it was given.
const REFUSALS: { call: string; ending: string; code: number; body: unknown; act: Act }[] = [
  { call: "the due queue", ending: "/outreach/due", code: 503, body: HELD, act: async () => {} },
  {
    call: "the contacts list",
    ending: "/outreach/contacts",
    code: 503,
    body: HELD,
    act: async () => {},
  },
  {
    call: "the candidates list",
    ending: "/candidates",
    code: 404,
    body: { detail: "no job matches 12 (an invented refusal)" },
    act: openJob,
  },
  {
    call: "Mark sent",
    ending: "/sent",
    code: 409,
    body: { detail: "cannot mark outreach sent (an invented refusal)" },
    act: (user) => clickInDueRow(user, "Mark sent"),
  },
  {
    call: "Mark replied",
    ending: "/replied",
    code: 409,
    body: { detail: "cannot mark outreach replied (an invented refusal)" },
    act: (user) => clickInDueRow(user, "Mark replied"),
  },
  {
    call: "Snooze",
    ending: "/snooze",
    code: 409,
    body: { detail: "cannot snooze until then (an invented refusal)" },
    act: (user) => clickInDueRow(user, "Snooze"),
  },
  {
    call: "Reject",
    ending: "/candidates/reject",
    code: 404,
    body: { detail: "candidate not staged (an invented refusal)" },
    act: async (user) => {
      await openJob(user);
      await user.click(await screen.findByRole("button", { name: "Reject" }));
    },
  },
  {
    call: "Find contacts",
    ending: "/find-contacts",
    code: 409,
    body: { detail: "a discovery run is active (an invented refusal)" },
    act: async (user) => {
      await openJob(user);
      await screen.findByRole("button", { name: "Approve" });
      await user.click(screen.getByRole("button", { name: "Find contacts" }));
    },
  },
];

test("each outreach call shows its refusal in the API's words", async () => {
  for (const refusal of REFUSALS) {
    stubApi({
      due: [dueRow()],
      candidates: [candidate()],
      refuse: { ending: refusal.ending, code: refusal.code, body: refusal.body },
    });
    vi.stubGlobal("prompt", () => "2026-12-01");
    const user = userEvent.setup();
    renderPage();

    await refusal.act(user);
    const detail = (refusal.body as { detail: string }).detail;
    await waitFor(() => {
      const alerts = screen.queryAllByRole("alert").map((alert) => alert.textContent);
      expect(
        alerts.some((text) => text.includes(detail)),
        refusal.call,
      ).toBe(true);
    });
    cleanup();
    vi.unstubAllGlobals();
  }
});

type ErrorOut = components["schemas"]["ErrorOut"];
type DatabaseHeldOut = components["schemas"]["DatabaseHeldOut"];

// The JSON body a response declares, or `never` when it declares none.
type Declared<Answer> = Answer extends { content: { "application/json": infer Body } }
  ? Body
  : never;

test("an outreach refusal is read through the contract's types", () => {
  // Checked by `pnpm type-check`, which compiles this file; when the test
  // runs, `expectTypeOf` does nothing. A reader that took `unknown` again,
  // or a call whose 404, 409 or 503 lost its declared body, does not compile.
  // `DatabaseHeldOut` fits `ErrorOut` by shape, so the reader's union is
  // pinned here by name (spec 129, Honest limitations).
  expectTypeOf(refused)
    .parameter(0)
    .toEqualTypeOf<ErrorOut | DatabaseHeldOut | components["schemas"]["HTTPValidationError"]>();

  expectTypeOf<
    Declared<operations["listOutreachDue"]["responses"][404]>
  >().toEqualTypeOf<ErrorOut>();
  expectTypeOf<
    Declared<operations["listOutreachDue"]["responses"][409]>
  >().toEqualTypeOf<ErrorOut>();
  expectTypeOf<
    Declared<operations["listCandidates"]["responses"][404]>
  >().toEqualTypeOf<ErrorOut>();
  expectTypeOf<
    Declared<operations["listCandidates"]["responses"][409]>
  >().toEqualTypeOf<ErrorOut>();
  expectTypeOf<
    Declared<operations["markOutreachSent"]["responses"][404]>
  >().toEqualTypeOf<ErrorOut>();
  expectTypeOf<
    Declared<operations["markOutreachSent"]["responses"][409]>
  >().toEqualTypeOf<ErrorOut>();
  expectTypeOf<
    Declared<operations["markOutreachReplied"]["responses"][404]>
  >().toEqualTypeOf<ErrorOut>();
  expectTypeOf<
    Declared<operations["markOutreachReplied"]["responses"][409]>
  >().toEqualTypeOf<ErrorOut>();
  expectTypeOf<
    Declared<operations["snoozeOutreach"]["responses"][404]>
  >().toEqualTypeOf<ErrorOut>();
  expectTypeOf<
    Declared<operations["snoozeOutreach"]["responses"][409]>
  >().toEqualTypeOf<ErrorOut>();
  expectTypeOf<
    Declared<operations["approveCandidate"]["responses"][404]>
  >().toEqualTypeOf<ErrorOut>();
  expectTypeOf<
    Declared<operations["approveCandidate"]["responses"][409]>
  >().toEqualTypeOf<ErrorOut>();
  expectTypeOf<
    Declared<operations["rejectCandidate"]["responses"][404]>
  >().toEqualTypeOf<ErrorOut>();
  expectTypeOf<
    Declared<operations["rejectCandidate"]["responses"][409]>
  >().toEqualTypeOf<ErrorOut>();
  expectTypeOf<Declared<operations["findContacts"]["responses"][404]>>().toEqualTypeOf<ErrorOut>();
  expectTypeOf<Declared<operations["findContacts"]["responses"][409]>>().toEqualTypeOf<ErrorOut>();

  expectTypeOf<
    Declared<operations["listOutreachDue"]["responses"][503]>
  >().toEqualTypeOf<DatabaseHeldOut>();
  expectTypeOf<
    Declared<operations["listOutreachContacts"]["responses"][503]>
  >().toEqualTypeOf<DatabaseHeldOut>();
  expectTypeOf<
    Declared<operations["listCandidates"]["responses"][503]>
  >().toEqualTypeOf<DatabaseHeldOut>();
  expectTypeOf<
    Declared<operations["markOutreachSent"]["responses"][503]>
  >().toEqualTypeOf<DatabaseHeldOut>();
  expectTypeOf<
    Declared<operations["markOutreachReplied"]["responses"][503]>
  >().toEqualTypeOf<DatabaseHeldOut>();
  expectTypeOf<
    Declared<operations["snoozeOutreach"]["responses"][503]>
  >().toEqualTypeOf<DatabaseHeldOut>();
  expectTypeOf<
    Declared<operations["approveCandidate"]["responses"][503]>
  >().toEqualTypeOf<DatabaseHeldOut>();
  expectTypeOf<
    Declared<operations["rejectCandidate"]["responses"][503]>
  >().toEqualTypeOf<DatabaseHeldOut>();
  expectTypeOf<
    Declared<operations["findContacts"]["responses"][503]>
  >().toEqualTypeOf<DatabaseHeldOut>();
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
