import type { components, operations } from "@harrier/contract";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, expectTypeOf, test, vi } from "vitest";

import { refusalMessage } from "../../shared/api/refusal";
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
      // `undefined` answers with no body at all, as a proxy or a crash might.
      const reply = (code: number, body: unknown): Promise<Response> =>
        record().then(() =>
          body === undefined
            ? new Response(null, { status: code, headers: { "Content-Length": "0" } })
            : new Response(JSON.stringify(body), {
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
  expect(alert.textContent).toBe("candidate not found in the staged artifact");
});

// --- refusals in the API's words --------------------------------------------

type ErrorOut = components["schemas"]["ErrorOut"];
type DatabaseHeldOut = components["schemas"]["DatabaseHeldOut"];
type HTTPValidationError = components["schemas"]["HTTPValidationError"];
type AuthRefusalOut = components["schemas"]["AuthRefusalOut"];
// Hosted, Storage unavailable on the routes that write the staged
// candidates (spec 107).
type StorageUnavailableOut = components["schemas"]["StorageUnavailableOut"];
type User = ReturnType<typeof userEvent.setup>;

async function clickInDueRow(user: User, name: string): Promise<void> {
  const row = (await screen.findByText("Northwind Labs")).closest("tr");
  expect(row).not.toBeNull();
  await user.click(within(row as HTMLElement).getByRole("button", { name }));
}

async function openJob(user: User): Promise<void> {
  await user.type(screen.getByLabelText("Job id"), "12");
}

const HELD: DatabaseHeldOut = {
  detail: "A host process holds the tracker database. This request is refused until it ends.",
  hold: { subcommand: "backup", since: "2000-01-01T00:00:00Z" },
};

// Every call site, a 422, which only a malformed request from this page
// produces, and two refusals that carry no words, which show the call's own
// fallback. Each message is invented, so seeing it proves the page showed the
// body it was given.
const REFUSALS: {
  call: string;
  ending: string;
  code: number;
  // `undefined` is a refusal with no body.
  body: ErrorOut | DatabaseHeldOut | HTTPValidationError | undefined;
  shown: string;
  act: (user: User) => Promise<void>;
}[] = [
  {
    call: "the due queue",
    ending: "/outreach/due",
    code: 503,
    body: HELD,
    shown: HELD.detail,
    act: async () => {},
  },
  {
    call: "the due queue, with no body",
    ending: "/outreach/due",
    code: 503,
    body: undefined,
    shown: "could not load the due queue",
    act: async () => {},
  },
  {
    call: "the contacts list",
    ending: "/outreach/contacts",
    code: 503,
    body: HELD,
    shown: HELD.detail,
    act: async () => {},
  },
  {
    call: "the candidates list",
    ending: "/candidates",
    code: 404,
    body: { detail: "no job matches 12 (an invented refusal)" },
    shown: "no job matches 12 (an invented refusal)",
    act: openJob,
  },
  {
    call: "Mark sent",
    ending: "/sent",
    code: 409,
    body: { detail: "cannot mark outreach sent (an invented refusal)" },
    shown: "cannot mark outreach sent (an invented refusal)",
    act: (user) => clickInDueRow(user, "Mark sent"),
  },
  {
    call: "Mark replied",
    ending: "/replied",
    code: 409,
    body: { detail: "cannot mark outreach replied (an invented refusal)" },
    shown: "cannot mark outreach replied (an invented refusal)",
    act: (user) => clickInDueRow(user, "Mark replied"),
  },
  {
    call: "Snooze",
    ending: "/snooze",
    code: 409,
    body: { detail: "cannot snooze until then (an invented refusal)" },
    shown: "cannot snooze until then (an invented refusal)",
    act: (user) => clickInDueRow(user, "Snooze"),
  },
  {
    // A 422 names the field it refused, in the shared reader's words.
    call: "Snooze, refused by the schema",
    ending: "/snooze",
    code: 422,
    body: { detail: [{ loc: ["body", "until"], msg: "an invented field error", type: "x" }] },
    shown: "until: an invented field error",
    act: (user) => clickInDueRow(user, "Snooze"),
  },
  {
    // A 422 that names no field leaves only the call's fallback.
    call: "Snooze, refused by the schema with no field named",
    ending: "/snooze",
    code: 422,
    body: { detail: [] },
    shown: "could not snooze",
    act: (user) => clickInDueRow(user, "Snooze"),
  },
  {
    call: "Approve",
    ending: "/candidates/approve",
    code: 404,
    body: { detail: "candidate not found in the staged artifact" },
    shown: "candidate not found in the staged artifact",
    act: async (user) => {
      await openJob(user);
      await user.click(await screen.findByRole("button", { name: "Approve" }));
    },
  },
  {
    call: "Reject",
    ending: "/candidates/reject",
    code: 404,
    body: { detail: "candidate not staged (an invented refusal)" },
    shown: "candidate not staged (an invented refusal)",
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
    shown: "a discovery run is active (an invented refusal)",
    act: async (user) => {
      await openJob(user);
      await screen.findByRole("button", { name: "Approve" });
      await user.click(screen.getByRole("button", { name: "Find contacts" }));
    },
  },
];

// One test per row, so one call site failing does not hide the rest. The
// spec citation check reads only fixed `test("...")` names, so spec 129 names
// these cases in prose rather than citing one.
test.each(REFUSALS)(
  "each outreach call shows its refusal in the API's words: $call",
  async ({ ending, code, body, shown, act }) => {
    const calls = stubApi({
      due: [dueRow()],
      candidates: [candidate()],
      refuse: { ending, code, body },
    });
    vi.stubGlobal("prompt", () => "2026-12-01");
    renderPage();

    await act(userEvent.setup());
    // Exactly the API's words: an alert that wrapped them would not match.
    await waitFor(() => {
      expect(screen.queryAllByRole("alert").map((alert) => alert.textContent)).toContain(shown);
    });
    if (ending === "/snooze") {
      // The date the operator gave is the one sent, for the row it was given on.
      const snoozed = calls.find((call) => call.url.endsWith("/snooze"));
      expect(snoozed?.url).toBe("/api/outreach/12/snooze");
      expect(snoozed?.body).toEqual({ until: "2026-12-01" });
    }
  },
);

// The JSON body a response declares, or `never` when it declares none.
type Declared<Answer> = Answer extends { content: { "application/json": infer Body } }
  ? Body
  : never;

test("an outreach refusal is read through the contract's types", () => {
  // Checked by `pnpm type-check`, which compiles this file; when the test
  // runs, `expectTypeOf` does nothing. A reader that took `unknown` again,
  // or a call whose 404, 409 or 503 lost its declared body, does not compile.
  // A 503 is the host lease's body or, hosted, the unavailable signing keys'
  // (spec 104).
  // `DatabaseHeldOut` fits `ErrorOut` by shape, so the reader's union is
  // pinned here by name (spec 129, Honest limitations). The hosted API's
  // token refusal joins it (spec 104).
  expectTypeOf(refusalMessage)
    .parameter(0)
    .toEqualTypeOf<
      | ErrorOut
      | DatabaseHeldOut
      | HTTPValidationError
      | AuthRefusalOut
      | components["schemas"]["CredentialRefusalOut"]
      | components["schemas"]["TelegramRefusalOut"]
    >();

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

  expectTypeOf<Declared<operations["listOutreachDue"]["responses"][503]>>().toEqualTypeOf<
    DatabaseHeldOut | AuthRefusalOut
  >();
  expectTypeOf<Declared<operations["listOutreachContacts"]["responses"][503]>>().toEqualTypeOf<
    DatabaseHeldOut | AuthRefusalOut
  >();
  expectTypeOf<Declared<operations["listCandidates"]["responses"][503]>>().toEqualTypeOf<
    DatabaseHeldOut | AuthRefusalOut
  >();
  expectTypeOf<Declared<operations["markOutreachSent"]["responses"][503]>>().toEqualTypeOf<
    DatabaseHeldOut | AuthRefusalOut
  >();
  expectTypeOf<Declared<operations["markOutreachReplied"]["responses"][503]>>().toEqualTypeOf<
    DatabaseHeldOut | AuthRefusalOut
  >();
  expectTypeOf<Declared<operations["snoozeOutreach"]["responses"][503]>>().toEqualTypeOf<
    DatabaseHeldOut | AuthRefusalOut
  >();
  expectTypeOf<Declared<operations["approveCandidate"]["responses"][503]>>().toEqualTypeOf<
    DatabaseHeldOut | AuthRefusalOut | StorageUnavailableOut
  >();
  expectTypeOf<Declared<operations["rejectCandidate"]["responses"][503]>>().toEqualTypeOf<
    DatabaseHeldOut | AuthRefusalOut | StorageUnavailableOut
  >();
  expectTypeOf<Declared<operations["findContacts"]["responses"][503]>>().toEqualTypeOf<
    DatabaseHeldOut | AuthRefusalOut
  >();

  // Hosted, a missing, invalid or expired token is a 401 (spec 104).
  expectTypeOf<
    Declared<operations["listOutreachDue"]["responses"][401]>
  >().toEqualTypeOf<AuthRefusalOut>();
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
