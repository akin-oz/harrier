import type { components, operations } from "@harrier/contract";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, expectTypeOf, test, vi } from "vitest";

import type { Job } from "../../entities/job";
import jobActionsCss from "../../features/tracker/JobActions.css?raw";
import { JobActions, refusalMessage } from "../../features/tracker/JobActions";
import { TrackerPage } from "./TrackerPage";

/**
 * The tracker page drives the same operations the CLI does (spec 042).
 *
 * What these hold is that the browser sends the domain's own words and shows
 * the domain's own answers. A UI that translated a verb into a status, ranked
 * a queue itself, or paraphrased a refusal would be a second implementation
 * of rules that already exist in one place, and it would drift silently
 * because both suites would still pass.
 */

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

type Row = Record<string, string | number>;

function job(id: number, company: string, score: string, status = "prospect"): Row {
  return {
    id,
    company,
    title: "Senior Frontend Engineer",
    url: `https://boards.example.com/x/${String(id)}`,
    location: "Remote, Europe",
    source: "greenhouse",
    status,
    score,
    fit_score: "",
    next_action: "",
    added_at: "2026-01-01",
  };
}

type Call = { url: string; method: string; body: unknown };

/**
 * Answers by route and records what was asked, so a test can assert on the
 * request the page made rather than only on what it rendered.
 */
function stubApi(options: {
  jobs?: Row[];
  queue?: Row[];
  status?: { code: number; body: unknown };
  outcome?: { code: number; body: unknown };
  // Answered only when set; otherwise a rescore is unstubbed, as before.
  rescore?: { code: number; body: unknown };
  add?: { code: number; body: unknown };
  // What a refetch returns once any write has been answered: a stale page.
  jobsAfterWrite?: Row[];
  // Holds every write open until it resolves, so a test can act while the
  // request is pending.
  hold?: Promise<void>;
}): Call[] {
  const calls: Call[] = [];
  let jobs = options.jobs ?? [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(String(input), init);
      const url = new URL(request.url);
      const record = async (): Promise<void> => {
        const raw = request.method === "GET" ? "" : await request.clone().text();
        calls.push({
          url: url.pathname + url.search,
          method: request.method,
          body: raw === "" ? null : JSON.parse(raw),
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
      if (url.pathname === "/api/jobs") return reply(200, jobs);
      const answered = (answer: { code: number; body: unknown }): Promise<Response> =>
        (options.hold ?? Promise.resolve()).then(() => {
          if (options.jobsAfterWrite !== undefined) jobs = options.jobsAfterWrite;
          return reply(answer.code, answer.body);
        });
      if (url.pathname === "/api/tracker/queue") return reply(200, options.queue ?? []);
      if (url.pathname.endsWith("/status")) {
        return answered(options.status ?? { code: 200, body: job(1, "Northwind", "80") });
      }
      if (url.pathname.endsWith("/outcome")) {
        return answered(options.outcome ?? { code: 200, body: job(1, "Northwind", "80") });
      }
      if (options.rescore !== undefined && url.pathname.endsWith("/rescore")) {
        return answered(options.rescore);
      }
      if (url.pathname === "/api/tracker") {
        const answer = options.add ?? {
          code: 200,
          body: { status: "added", message: "added", job: job(9, "Northwind", "70") },
        };
        return reply(answer.code, answer.body);
      }
      return reply(404, { detail: `unstubbed ${url.pathname}` });
    }),
  );
  return calls;
}

function renderPage(): void {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <TrackerPage />
    </QueryClientProvider>,
  );
}

async function rowFor(company: string): Promise<HTMLElement> {
  const cell = await screen.findByText(company);
  const row = cell.closest("tr");
  if (row === null) throw new Error(`no row for ${company}`);
  return row;
}

// --- the browser sends the CLI's verb ----------------------------------------

test("a status button sends the verb, not a status the UI picked", async () => {
  const calls = stubApi({ jobs: [job(1, "Northwind", "80")] });
  const user = userEvent.setup();
  renderPage();

  const row = await rowFor("Northwind");
  await user.click(within(row).getByRole("button", { name: "Shortlist" }));

  await waitFor(() => {
    expect(calls.some((call) => call.url === "/api/tracker/1/status")).toBe(true);
  });
  const sent = calls.find((call) => call.url === "/api/tracker/1/status");
  // `shortlist`, not `shortlisted`. The mapping from one to the other lives in
  // `harrier.tracker.actions.STATUS_BY_VERB` and this is the page declining to
  // own a second copy of it.
  expect(sent?.body).toEqual({ verb: "shortlist", reason: null });
});

test("every verb the CLI has is reachable on the page", async () => {
  // What to do next for this status sits on the row; the rest are behind the
  // disclosure. The property is that none of them is gone, so this opens it
  // and then asserts the whole set, rather than asserting that all five are
  // visible at once. The CLI's `interviewing` is reached as Interview invite:
  // an interview is the company's outcome, so the page names it as one
  // (spec 080).
  stubApi({ jobs: [job(1, "Northwind", "80")] });
  const user = userEvent.setup();
  renderPage();
  const row = await rowFor("Northwind");

  await user.click(within(row).getByRole("button", { name: /^More actions/ }));
  for (const label of [
    "Shortlist",
    "Request CV",
    "Applied",
    "Interview invite",
    "Reject",
    "Rescore",
  ]) {
    expect(
      within(row).getByRole("button", { name: label }),
      `${label} is not reachable on the row`,
    ).toBeDefined();
  }
});

test("a refusal is shown in the words the API used", async () => {
  // Not "something went wrong": the operator needs the reason the tracker
  // gave, which is the same sentence the command line prints.
  stubApi({
    jobs: [job(1, "Northwind", "80")],
    status: { code: 409, body: { detail: "a reason is only recorded on a rejection" } },
  });
  const user = userEvent.setup();
  renderPage();

  const row = await rowFor("Northwind");
  await user.click(within(row).getByRole("button", { name: "Shortlist" }));

  expect(await screen.findByText("a reason is only recorded on a rejection")).toBeDefined();
});

test("rejecting asks for a reason before it sends anything", async () => {
  const calls = stubApi({ jobs: [job(1, "Northwind", "80")] });
  const user = userEvent.setup();
  renderPage();

  const row = await rowFor("Northwind");
  await user.click(within(row).getByRole("button", { name: "Reject" }));
  expect(calls.some((call) => call.url.endsWith("/status"))).toBe(false);

  // The free-text path lives behind `other…` now; a typed reason still
  // travels exactly as before (spec 056).
  await user.click(within(row).getByRole("button", { name: "other…" }));
  await user.type(within(row).getByLabelText("Rejection reason"), "wrong stack");
  await user.click(within(row).getByRole("button", { name: "Confirm" }));

  // With the code its select chose, `other` unless changed (spec 080).
  await waitFor(() => {
    const sent = calls.find((call) => call.url === "/api/tracker/1/status");
    expect(sent?.body).toEqual({ verb: "reject", reason: "wrong stack", reason_code: "other" });
  });
});

test("a reason pill submits the rejection in one click", async () => {
  // The pill is the confirmation: no second control stands between the
  // operator and the frequent case (spec 056).
  const calls = stubApi({ jobs: [job(1, "Northwind", "80")] });
  const user = userEvent.setup();
  renderPage();

  const row = await rowFor("Northwind");
  await user.click(within(row).getByRole("button", { name: "Reject" }));
  await user.click(within(row).getByRole("button", { name: "hybrid" }));

  await waitFor(() => {
    const sent = calls.find((call) => call.url === "/api/tracker/1/status");
    expect(sent?.body).toEqual({ verb: "reject", reason: "hybrid", reason_code: "not_remote" });
  });
});

test("every pill submits its exact lowercase label as the reason", async () => {
  // The strings are the stored values; consistent spellings are what makes
  // rejection_reason groupable later (spec 056). `rejected by company` is no
  // longer among them: it is the company's response, recorded through
  // Company replied (spec 080).
  for (const why of ["hybrid", "onsite", "closed", "missing stack", "location", "language"]) {
    cleanup();
    const calls = stubApi({ jobs: [job(1, "Northwind", "80")] });
    const user = userEvent.setup();
    renderPage();

    const row = await rowFor("Northwind");
    await user.click(within(row).getByRole("button", { name: "Reject" }));
    await user.click(within(row).getByRole("button", { name: why }));

    await waitFor(() => {
      const sent = calls.find((call) => call.url === "/api/tracker/1/status");
      expect((sent?.body as { reason?: string } | undefined)?.reason, `${why} changed`).toBe(why);
    });
  }
});

test("choosing other moves focus into the reason input", async () => {
  // The click unmounts the button that had focus; a keyboard user must land
  // in the input the click asked for, not on nothing (review finding on
  // PR #65).
  stubApi({ jobs: [job(1, "Northwind", "80")] });
  const user = userEvent.setup();
  renderPage();

  const row = await rowFor("Northwind");
  await user.click(within(row).getByRole("button", { name: "Reject" }));
  await user.click(within(row).getByRole("button", { name: "other…" }));

  const input = within(row).getByLabelText("Rejection reason");
  await waitFor(() => {
    expect(document.activeElement).toBe(input);
  });
});

test("cancelling the pills closes the picker without posting", async () => {
  const calls = stubApi({ jobs: [job(1, "Northwind", "80")] });
  const user = userEvent.setup();
  renderPage();

  const row = await rowFor("Northwind");
  await user.click(within(row).getByRole("button", { name: "Reject" }));
  await user.click(within(row).getByRole("button", { name: "Cancel" }));

  expect(calls.some((call) => call.url.endsWith("/status"))).toBe(false);
  expect(within(row).queryByRole("button", { name: "hybrid" })).toBeNull();
  expect(within(row).getByRole("button", { name: "Shortlist" })).toBeDefined();
});

// --- a rejected row can be reopened (spec 072) --------------------------------

test("a rejected row offers Reopen, which shortlists it", async () => {
  // The batch evaluator rejects rows on its own. When a recruiter writes
  // about one later, the row needs a way back that the page offers.
  const calls = stubApi({ jobs: [job(1, "Northwind", "80", "rejected")] });
  const user = userEvent.setup();
  renderPage();

  const row = await rowFor("Northwind");
  await user.click(within(row).getByRole("button", { name: "Reopen" }));

  await waitFor(() => {
    const sent = calls.find((call) => call.url === "/api/tracker/1/status");
    expect(sent?.body).toEqual({ verb: "shortlist", reason: null });
  });
});

test("a rejected row can move straight to interviewing", async () => {
  // A recruiter who writes after a rejection (spec 072). The move is
  // recorded as what it is, the company's invitation, through the outcome
  // route rather than as a status the candidate chose (spec 080).
  const calls = stubApi({ jobs: [job(1, "Northwind", "80", "rejected")] });
  const user = userEvent.setup();
  renderPage();

  const row = await rowFor("Northwind");
  await user.click(within(row).getByRole("button", { name: /^More actions/ }));
  await user.click(within(row).getByRole("button", { name: "Interview invite" }));

  await waitFor(() => {
    const sent = calls.find((call) => call.url === "/api/tracker/1/outcome");
    expect(sent?.body).toEqual({ code: "interview_invited", note: null });
  });
});

test("a rejected row cannot be rejected again or applied to", async () => {
  stubApi({ jobs: [job(1, "Northwind", "80", "rejected")] });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <TrackerPage onApply={() => undefined} />
    </QueryClientProvider>,
  );

  const row = await rowFor("Northwind");
  const reject = within(row).getByRole("button", { name: "Reject" });
  const apply = within(row).getByRole("button", { name: /^Apply to Northwind/ });
  expect((reject as HTMLButtonElement).disabled).toBe(true);
  expect((apply as HTMLButtonElement).disabled).toBe(true);
});

// --- the queue ordering is the domain's answer -------------------------------

test("the queue view renders the server's ranking rather than re-sorting it", async () => {
  // The queue answers with the domain's ordering, and here the top row scores
  // lowest. A table that sorted by score would put Zephyr first and quietly
  // replace `next` with its own idea of what to work on.
  const calls = stubApi({
    jobs: [job(1, "Aurora", "10"), job(2, "Zephyr", "99")],
    queue: [job(1, "Aurora", "10"), job(2, "Zephyr", "99")],
  });
  const user = userEvent.setup();
  renderPage();

  await screen.findByText("Aurora");
  await user.click(screen.getByRole("button", { name: "Next up" }));

  await waitFor(() => {
    expect(calls.some((call) => call.url.startsWith("/api/tracker/queue"))).toBe(true);
  });
  await waitFor(() => {
    // The company element, not the whole cell: company and title share one
    // column now. The ranking being asserted is unchanged.
    const companies = screen
      .getAllByRole("row")
      .slice(1)
      .map((row) => row.querySelector(".job-table__company")?.textContent);
    expect(companies).toEqual(["Aurora", "Zephyr"]);
  });
});

test("the two queues are different questions and ask them differently", async () => {
  const calls = stubApi({ jobs: [job(1, "Aurora", "10")], queue: [job(1, "Aurora", "10")] });
  const user = userEvent.setup();
  renderPage();
  await screen.findByText("Aurora");

  await user.click(screen.getByRole("button", { name: "Next up" }));
  await waitFor(() => {
    expect(calls.some((call) => call.url === "/api/tracker/queue?undecided=false")).toBe(true);
  });

  await user.click(screen.getByRole("button", { name: "Needs a decision" }));
  await waitFor(() => {
    expect(calls.some((call) => call.url === "/api/tracker/queue?undecided=true")).toBe(true);
  });
});

// --- adding by hand -----------------------------------------------------------

test("a duplicate is reported in the domain's words and the form keeps its input", async () => {
  stubApi({
    add: { code: 200, body: { status: "duplicate", message: "already tracked", job: null } },
  });
  const user = userEvent.setup();
  renderPage();

  await user.click(screen.getByRole("button", { name: "Add a job by hand" }));
  await user.type(screen.getByLabelText("Company"), "Northwind");
  await user.type(screen.getByLabelText("Title"), "Senior Frontend Engineer");
  await user.click(screen.getByRole("button", { name: "Add" }));

  expect(await screen.findByText("already tracked")).toBeDefined();
  // Nothing was added, so clearing the form would throw away work the
  // operator would have to type again to correct.
  expect(screen.getByLabelText("Company")).toHaveProperty("value", "Northwind");
});

test("a successful add clears the form and refetches the tracker", async () => {
  const calls = stubApi({});
  const user = userEvent.setup();
  renderPage();

  await user.click(screen.getByRole("button", { name: "Add a job by hand" }));
  await user.type(screen.getByLabelText("Company"), "Northwind");
  await user.type(screen.getByLabelText("Title"), "Senior Frontend Engineer");
  const before = calls.filter((call) => call.url === "/api/jobs").length;
  await user.click(screen.getByRole("button", { name: "Add" }));

  await waitFor(() => {
    expect(screen.getByLabelText("Company")).toHaveProperty("value", "");
  });
  await waitFor(() => {
    expect(calls.filter((call) => call.url === "/api/jobs").length).toBeGreaterThan(before);
  });
});

// --- the writes carry the local token -----------------------------------------

test("every tracker write carries the token and no read does", async () => {
  // Spec 042 declares spec 035 a hard dependency: these buttons reach
  // destructive writes from any page open in the browser.
  //
  // All three writes, not one. This used to send only the status change while
  // claiming to cover every write, so a token regression in the manual add or
  // the rescore would have passed it (review finding on PR #41).
  const seen: { url: string; method: string; token: string | null }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(String(input), init);
      const url = new URL(request.url);
      seen.push({
        url: url.pathname,
        method: request.method,
        token: request.headers.get("X-Harrier-Token"),
      });
      const body =
        url.pathname === "/api/session"
          ? { token: "test-token" }
          : url.pathname === "/api/jobs"
            ? [job(1, "Northwind", "80")]
            : url.pathname === "/api/tracker"
              ? { status: "added", message: "added", job: job(9, "Northwind", "70") }
              : url.pathname.endsWith("/rescore")
                ? { previous: "80", current: 81, job: job(1, "Northwind", "81") }
                : job(1, "Northwind", "80");
      return Promise.resolve(
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    }),
  );
  const user = userEvent.setup();
  renderPage();

  const row = await rowFor("Northwind");
  await user.click(within(row).getByRole("button", { name: "Shortlist" }));
  await user.click(within(row).getByRole("button", { name: /^More actions/ }));
  await user.click(within(row).getByRole("button", { name: "Rescore" }));
  await user.click(screen.getByRole("button", { name: "Add a job by hand" }));
  await user.type(screen.getByLabelText("Company"), "Alder");
  await user.type(screen.getByLabelText("Title"), "Senior Frontend Engineer");
  await user.click(screen.getByRole("button", { name: "Add" }));

  const writes = ["/api/tracker/1/status", "/api/tracker/1/rescore", "/api/tracker"];
  for (const path of writes) {
    await waitFor(() => {
      expect(seen.some((call) => call.url === path && call.method === "POST")).toBe(true);
    });
    const call = seen.find((entry) => entry.url === path && entry.method === "POST");
    expect(call?.token, `${path} did not carry the token`).toBe("test-token");
  }
  // And no read carries it: sending it to every GET would gain nothing and
  // make it that much easier to leak.
  //
  // The count is asserted first. `every` over an empty array is true, so a
  // change that stopped the page reading anything at all would have satisfied
  // this line rather than failed it (spec 045).
  const reads = seen.filter((call) => call.method === "GET");
  expect(reads.length).toBeGreaterThan(0);
  expect(reads.every((call) => call.token === null)).toBe(true);
});

test("the other verbs are out of reach while a rejection reason is being typed", async () => {
  // The row is mid-decision. They used to stay live, so a click could land a
  // status change on a row the operator was in the middle of rejecting
  // (review finding on PR #41).
  stubApi({ jobs: [job(1, "Northwind", "80")] });
  const user = userEvent.setup();
  renderPage();

  const row = await rowFor("Northwind");
  // Opened first, so Rescore is on screen and its disappearance below is the
  // rejection hiding it rather than the disclosure never having been open.
  await user.click(within(row).getByRole("button", { name: /^More actions/ }));
  expect(within(row).getByRole("button", { name: "Shortlist" })).toBeDefined();
  expect(within(row).getByRole("button", { name: "Rescore" })).toBeDefined();

  await user.click(within(row).getByRole("button", { name: "Reject" }));
  expect(within(row).queryByRole("button", { name: "Shortlist" })).toBeNull();
  expect(within(row).queryByRole("button", { name: "Rescore" })).toBeNull();

  await user.click(within(row).getByRole("button", { name: "Cancel" }));
  // Both, not just one. Asserting only Shortlist let a regression that kept
  // Rescore hidden after cancelling pass (review finding on PR #41).
  expect(within(row).getByRole("button", { name: "Shortlist" })).toBeDefined();
  expect(within(row).getByRole("button", { name: "Rescore" })).toBeDefined();
});

// --- who decided: the candidate's exit and the company's response (spec 080) --

// What each exit pill sends: the code the history records, and the text the
// row keeps, exactly as spec 056 stored it.
const EXIT_PILL_CODES: readonly (readonly [string, string])[] = [
  ["hybrid", "not_remote"],
  ["onsite", "not_remote"],
  ["closed", "vacancy_closed"],
  ["missing stack", "stack"],
  ["location", "location"],
  ["language", "language"],
];
const COMPANY_PILL_TEXTS = [
  "interview",
  "rejected",
  "assessment failed",
  "ghosted",
  "no response",
  "rejected by company",
];
const COMPANY_CODES = [
  "company_rejected",
  "ghosted",
  "no_response",
  "assessment_failed",
  "interview_invited",
];

function renderWithApply(): void {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <TrackerPage onApply={() => undefined} />
    </QueryClientProvider>,
  );
}

test("each pill sends its code and text", async () => {
  for (const [text, code] of EXIT_PILL_CODES) {
    cleanup();
    const calls = stubApi({ jobs: [job(1, "Northwind", "80")] });
    const user = userEvent.setup();
    renderPage();

    const row = await rowFor("Northwind");
    await user.click(within(row).getByRole("button", { name: "Reject" }));
    await user.click(within(row).getByRole("button", { name: text }));

    await waitFor(() => {
      const sent = calls.find((call) => call.url === "/api/tracker/1/status");
      expect(sent?.body, text).toEqual({ verb: "reject", reason: text, reason_code: code });
    });
  }
});

test("the exit controls offer no company verdict", async () => {
  // Neither the pills nor the select behind `other…` can file what a company
  // did as the candidate's own decision, before applying or after.
  for (const [status, exit] of [
    ["prospect", "Reject"],
    ["applied", "Withdraw"],
  ] as const) {
    cleanup();
    stubApi({ jobs: [job(1, "Northwind", "80", status)] });
    const user = userEvent.setup();
    renderPage();

    const row = await rowFor("Northwind");
    await user.click(within(row).getByRole("button", { name: exit }));
    const group = within(row).getByRole("group", { name: exit });
    for (const text of COMPANY_PILL_TEXTS) {
      expect(
        within(group).queryByRole("button", { name: text }),
        `${exit} offers ${text}`,
      ).toBeNull();
    }

    await user.click(within(group).getByRole("button", { name: "other…" }));
    const select = within(row).getByLabelText("Reason code");
    const values = Array.from((select as HTMLSelectElement).options).map((option) => option.value);
    expect(values.length).toBeGreaterThan(0);
    for (const code of COMPANY_CODES) {
      expect(values, `${exit} offers ${code}`).not.toContain(code);
    }
  }
});

test("other sends the selected code", async () => {
  const calls = stubApi({ jobs: [job(1, "Northwind", "80")] });
  const user = userEvent.setup();
  renderPage();

  const row = await rowFor("Northwind");
  await user.click(within(row).getByRole("button", { name: "Reject" }));
  await user.click(within(row).getByRole("button", { name: "other…" }));

  const select = within(row).getByLabelText("Reason code");
  expect((select as HTMLSelectElement).value).toBe("other");
  // `other` says nothing without words, so it waits for them.
  const confirm = within(row).getByRole("button", { name: "Confirm" });
  expect((confirm as HTMLButtonElement).disabled).toBe(true);

  await user.selectOptions(select, "role_too_senior");
  await user.type(within(row).getByLabelText("Rejection reason"), "staff level role");
  await user.click(within(row).getByRole("button", { name: "Confirm" }));

  await waitFor(() => {
    const sent = calls.find((call) => call.url === "/api/tracker/1/status");
    expect(sent?.body).toEqual({
      verb: "reject",
      reason: "staff level role",
      reason_code: "role_too_senior",
    });
  });
});

test("a chosen code needs no words", async () => {
  // Only `other` waits for text. Any other code confirms at once and sends its
  // own label as the reason, so the row still reads as one (spec 080).
  const calls = stubApi({ jobs: [job(1, "Northwind", "80")] });
  const user = userEvent.setup();
  renderPage();

  const row = await rowFor("Northwind");
  await user.click(within(row).getByRole("button", { name: "Reject" }));
  await user.click(within(row).getByRole("button", { name: "other…" }));
  await user.selectOptions(within(row).getByLabelText("Reason code"), "role_too_senior");
  await user.click(within(row).getByRole("button", { name: "Confirm" }));

  await waitFor(() => {
    const sent = calls.find((call) => call.url === "/api/tracker/1/status");
    expect(sent?.body).toEqual({
      verb: "reject",
      reason: "too senior",
      reason_code: "role_too_senior",
    });
  });
});

test("the exit word names who acted", async () => {
  // Before applying, leaving is a rejection. After, it is a withdrawal, and
  // it sits beside the control for what the company said (spec 080).
  for (const [status, present, absent] of [
    ["prospect", ["Reject"], ["Withdraw", "Company replied"]],
    ["shortlisted", ["Reject"], ["Withdraw", "Company replied"]],
    ["tailored_cv_requested", ["Reject"], ["Withdraw", "Company replied"]],
    ["applied", ["Withdraw", "Company replied"], ["Reject"]],
    ["interviewing", ["Withdraw", "Company replied"], ["Reject"]],
  ] as const) {
    cleanup();
    stubApi({ jobs: [job(1, "Northwind", "80", status)] });
    renderPage();

    const row = await rowFor("Northwind");
    for (const label of present) {
      expect(
        within(row).queryByRole("button", { name: label }),
        `${status}: ${label}`,
      ).not.toBeNull();
    }
    for (const label of absent) {
      expect(within(row).queryByRole("button", { name: label }), `${status}: ${label}`).toBeNull();
    }
  }
});

test("the resting row does not grow", async () => {
  // One forward control, the exit, Apply and More: the shape the actions
  // column was narrowed to so the table stops scrolling sideways. Nothing in
  // spec 080 adds to it, and no pill shows until a takeover is opened.
  for (const status of [
    "prospect",
    "shortlisted",
    "tailored_cv_requested",
    "applied",
    "interviewing",
    "rejected",
  ]) {
    cleanup();
    stubApi({ jobs: [job(1, "Northwind", "80", status)] });
    renderWithApply();

    const row = await rowFor("Northwind");
    const resting = row.querySelector(".job-actions__row");
    expect(resting, status).not.toBeNull();
    const buttons = within(resting as HTMLElement).getAllByRole("button");
    expect(
      buttons.length,
      `${status} shows ${String(buttons.length)} controls`,
    ).toBeLessThanOrEqual(4);
    expect(row.querySelector(".job-actions__pill"), status).toBeNull();
  }
});

test("company replied submits the company outcome", async () => {
  const calls = stubApi({ jobs: [job(1, "Northwind", "80", "applied")] });
  const user = userEvent.setup();
  renderPage();

  const row = await rowFor("Northwind");
  await user.click(within(row).getByRole("button", { name: "Company replied" }));
  const group = within(row).getByRole("group", { name: "Company response" });
  expect(
    within(group)
      .getAllByRole("button")
      .map((button) => button.textContent),
  ).toEqual(["interview", "rejected", "assessment failed", "ghosted", "no response", "Cancel"]);

  await user.click(within(group).getByRole("button", { name: "ghosted" }));
  await waitFor(() => {
    const sent = calls.find((call) => call.url === "/api/tracker/1/outcome");
    expect(sent?.body).toEqual({ code: "ghosted", note: null });
  });
  // Through its own route only: nothing reached the candidate's status verb.
  expect(calls.some((call) => call.url.endsWith("/status"))).toBe(false);

  // Once interviewing, the invitation has happened and silence is no longer
  // the question.
  cleanup();
  stubApi({ jobs: [job(1, "Northwind", "80", "interviewing")] });
  renderPage();
  const interviewing = await rowFor("Northwind");
  await user.click(within(interviewing).getByRole("button", { name: "Company replied" }));
  const offered = within(interviewing).getByRole("group", { name: "Company response" });
  expect(
    within(offered)
      .getAllByRole("button")
      .map((button) => button.textContent),
  ).toEqual(["rejected", "assessment failed", "ghosted", "Cancel"]);
});

test("danger marks the pills that close the row", async () => {
  // Color means consequence, not actor (spec 080).
  stubApi({ jobs: [job(1, "Northwind", "80", "applied")] });
  const user = userEvent.setup();
  renderPage();

  const row = await rowFor("Northwind");
  await user.click(within(row).getByRole("button", { name: "Company replied" }));
  const closes = (name: string): boolean =>
    within(row).getByRole("button", { name }).classList.contains("job-actions__pill--closes");
  expect(closes("interview")).toBe(false);
  for (const name of ["rejected", "assessment failed", "ghosted", "no response"]) {
    expect(closes(name), name).toBe(true);
  }

  await user.click(within(row).getByRole("button", { name: "Cancel" }));
  await user.click(within(row).getByRole("button", { name: "Withdraw" }));
  for (const [text] of EXIT_PILL_CODES) {
    expect(closes(text), text).toBe(true);
  }
});

/**
 * `[ids, classes, types]` for one selector, `:not(x)` counting as `x`. Enough
 * for the plain selectors JobActions.css uses: no `:is()`, `:where()` or
 * nesting, which this would miscount.
 */
function specificity(selector: string): number[] {
  const tokens =
    selector
      .replace(/:not\(([^)]*)\)/g, " $1")
      .match(/#[\w-]+|\.[\w-]+|::[\w-]+|:[\w-]+|\[[^\]]*\]|\b[a-z][\w-]*/gi) ?? [];
  const count = (test: (token: string) => boolean): number => tokens.filter(test).length;
  return [
    count((token) => token.startsWith("#")),
    count((token) => /^(\.|\[|:(?!:))/.test(token)),
    count((token) => /^(::|[a-z])/i.test(token)),
  ];
}

function outranks(a: number[], b: number[]): boolean {
  const index = a.findIndex((value, at) => value !== b[at]);
  return index !== -1 && (a[index] ?? 0) > (b[index] ?? 0);
}

/** JobActions.css as jsdom parses it: its rules, not what they compute to. */
function jobActionsRules(): CSSStyleRule[] {
  const style = document.createElement("style");
  style.textContent = jobActionsCss;
  document.head.append(style);
  const rules = Array.from(style.sheet?.cssRules ?? []).filter(
    (rule): rule is CSSStyleRule => "selectorText" in rule,
  );
  style.remove();
  return rules;
}

/**
 * The selectors in JobActions.css that style a button in `scope` at rest but
 * lose to `.job-actions button`, which sets the border, font and color of
 * every one of them. A browser never applies such a rule, whatever jsdom
 * computes. A selector with a pseudo-class styles a state rather than the
 * rest and is left out; the hovers are held by "a danger hover outranks the
 * ordinary hover".
 */
function restingRulesThatLose(scope: HTMLElement): string[] {
  const rules = jobActionsRules();
  const shared = rules.find((rule) => rule.selectorText === ".job-actions button");
  if (shared === undefined) throw new Error("the shared button rule is gone");
  const buttons = Array.from(scope.querySelectorAll(".job-actions button"));
  return rules
    .filter((rule) => rule !== shared)
    .flatMap((rule) => rule.selectorText.split(",").map((selector) => selector.trim()))
    .filter(
      (selector) =>
        !selector.includes(":") &&
        buttons.some((button) => button.matches(selector)) &&
        !outranks(specificity(selector), specificity(shared.selectorText)),
    );
}

test("a danger hover outranks the ordinary hover", () => {
  // jsdom applies matching rules in source order and ignores specificity,
  // so no rendered test can see this, and "danger marks the pills that close
  // the row" passed while a browser showed the ordinary accent on every one
  // of them. The cascade is checked on the stylesheet's own rules, as jsdom
  // parses them (review of PR #122).
  const rules = jobActionsRules();

  const ordinary = rules.find(
    (rule) => rule.selectorText === ".job-actions button:hover:not(:disabled)",
  );
  if (ordinary === undefined) throw new Error("the ordinary hover rule is gone");
  const danger = rules.filter(
    (rule) =>
      rule.selectorText.includes(":hover") &&
      rule.style.getPropertyValue("color").includes("--color-danger"),
  );
  // The exit control and the pills that close the row.
  expect(danger.length).toBe(2);
  for (const rule of danger) {
    expect(
      outranks(specificity(rule.selectorText), specificity(ordinary.selectorText)),
      rule.selectorText,
    ).toBe(true);
  }
});

// The forward control each status shows on its resting row.
const FORWARD_CONTROLS: readonly (readonly [string, string])[] = [
  ["prospect", "Shortlist"],
  ["shortlisted", "Request CV"],
  ["tailored_cv_requested", "Applied"],
  ["applied", "Company replied"],
  ["interviewing", "Company replied"],
  ["rejected", "Reopen"],
];

test("the forward control's look outranks the shared button rule", async () => {
  // jsdom applied the look and a browser never did: `.job-actions button`
  // outranks a bare class selector, so the forward control looked like every
  // other control on the row (spec 047). The look is found by what it
  // declares, so a renamed selector is held to the same rule.
  const look = jobActionsRules().find(
    (rule) =>
      rule.style.getPropertyValue("font-weight") === "500" &&
      rule.style.getPropertyValue("border-color").includes("--color-border-strong"),
  );
  if (look === undefined) throw new Error("the forward control's look is gone");

  for (const [status, forward] of FORWARD_CONTROLS) {
    cleanup();
    stubApi({ jobs: [job(1, "Northwind", "80", status)] });
    const user = userEvent.setup();
    renderWithApply();

    const row = await rowFor("Northwind");
    const marked = within(row)
      .getAllByRole("button")
      .filter((button) => button.matches(look.selectorText))
      .map((button) => button.textContent);
    expect(marked, status).toEqual([forward]);

    // With More open, every button a resting row can show is on the page.
    await user.click(within(row).getByRole("button", { name: /^More actions/ }));
    expect(restingRulesThatLose(row), status).toEqual([]);
  }
});

test("other…'s muted color outranks the shared button rule", async () => {
  // jsdom applied the muted color and a browser never did: `.job-actions
  // button` outranks a bare class selector, so other… read in the pills' own
  // color (spec 056). The rule is found by what it declares, and it mutes
  // other… alone, not a pill.
  stubApi({ jobs: [job(1, "Northwind", "80")] });
  const user = userEvent.setup();
  renderPage();

  const row = await rowFor("Northwind");
  await user.click(within(row).getByRole("button", { name: "Reject" }));
  const takeover = within(row).getByRole("group", { name: "Reject" });
  const other = within(takeover).getByRole("button", { name: "other…" });
  const muted = jobActionsRules().find(
    (rule) =>
      rule.style.getPropertyValue("color").includes("--color-text-secondary") &&
      other.matches(rule.selectorText),
  );
  if (muted === undefined) throw new Error("other…'s muted color is gone");
  expect(
    within(takeover)
      .getAllByRole("button")
      .filter((button) => button.matches(muted.selectorText))
      .map((button) => button.textContent),
  ).toEqual(["other…"]);
  expect(restingRulesThatLose(takeover)).toEqual([]);

  // The reason form behind other… holds the rest of the takeover's buttons.
  await user.click(other);
  expect(restingRulesThatLose(within(row).getByRole("group", { name: "Reject" }))).toEqual([]);
});

test("other… stays a group named by its exit word", async () => {
  // Whose decision it is stays announced in every state of the takeover,
  // including the reason form behind other… (spec 080).
  for (const [status, word] of [
    ["prospect", "Reject"],
    ["applied", "Withdraw"],
  ] as const) {
    cleanup();
    stubApi({ jobs: [job(1, "Northwind", "80", status)] });
    const user = userEvent.setup();
    renderPage();
    const row = await rowFor("Northwind");
    await user.click(within(row).getByRole("button", { name: word }));
    await user.click(within(row).getByRole("button", { name: "other…" }));
    const group = within(row).getByRole("group", { name: word });
    expect(within(group).getByLabelText("Reason code")).toBeDefined();
  }
});

test("interviewing is a company outcome, not a verb", async () => {
  for (const status of ["prospect", "applied"]) {
    cleanup();
    stubApi({ jobs: [job(1, "Northwind", "80", status)] });
    const user = userEvent.setup();
    renderPage();
    const row = await rowFor("Northwind");
    await user.click(within(row).getByRole("button", { name: /^More actions/ }));
    expect(within(row).queryByRole("button", { name: "Interviewing" }), status).toBeNull();
  }

  cleanup();
  const calls = stubApi({ jobs: [job(1, "Northwind", "80", "applied")] });
  const user = userEvent.setup();
  renderPage();
  const row = await rowFor("Northwind");
  await user.click(within(row).getByRole("button", { name: "Company replied" }));
  await user.click(within(row).getByRole("button", { name: "interview" }));

  await waitFor(() => {
    const sent = calls.find((call) => call.url === "/api/tracker/1/outcome");
    expect(sent?.body).toEqual({ code: "interview_invited", note: null });
  });
  expect(calls.some((call) => call.url.endsWith("/status"))).toBe(false);
});

test("a takeover keeps keyboard focus", async () => {
  stubApi({ jobs: [job(1, "Northwind", "80")] });
  const user = userEvent.setup();
  renderPage();
  const row = await rowFor("Northwind");

  // Opening lands on the first pill rather than on nothing.
  await user.click(within(row).getByRole("button", { name: "Reject" }));
  await waitFor(() => {
    expect(document.activeElement).toBe(within(row).getByRole("button", { name: "hybrid" }));
  });
  // Escape closes without sending and hands focus back to the opener.
  await user.keyboard("{Escape}");
  expect(within(row).queryByRole("button", { name: "hybrid" })).toBeNull();
  await waitFor(() => {
    expect(document.activeElement).toBe(within(row).getByRole("button", { name: "Reject" }));
  });
  // So does Cancel.
  await user.click(within(row).getByRole("button", { name: "Reject" }));
  await user.click(within(row).getByRole("button", { name: "Cancel" }));
  await waitFor(() => {
    expect(document.activeElement).toBe(within(row).getByRole("button", { name: "Reject" }));
  });

  // The company's takeover behaves the same way.
  cleanup();
  stubApi({ jobs: [job(1, "Northwind", "80", "applied")] });
  renderPage();
  const applied = await rowFor("Northwind");
  await user.click(within(applied).getByRole("button", { name: "Company replied" }));
  await waitFor(() => {
    expect(document.activeElement).toBe(within(applied).getByRole("button", { name: "interview" }));
  });
  await user.keyboard("{Escape}");
  await waitFor(() => {
    expect(document.activeElement).toBe(
      within(applied).getByRole("button", { name: "Company replied" }),
    );
  });
});

test("a refused company response takes focus to its reason", async () => {
  // A stale page can offer Company replied on a row that has since changed.
  // The refusal closes the takeover and refetches the row, and focus goes to
  // the words that explain it. It went back to Company replied once (review
  // finding on PR #122), but the refetch can turn that very button into
  // Reopen, and Enter then sent a verb nobody chose.
  stubApi({
    jobs: [job(1, "Northwind", "80", "applied")],
    jobsAfterWrite: [job(1, "Northwind", "80", "rejected")],
    outcome: { code: 409, body: { detail: "no application was recorded for this job" } },
  });
  const user = userEvent.setup();
  renderPage();

  const row = await rowFor("Northwind");
  await user.click(within(row).getByRole("button", { name: "Company replied" }));
  await user.click(within(row).getByRole("button", { name: "ghosted" }));

  const reason = await screen.findByText("no application was recorded for this job");
  await within(row).findByRole("button", { name: "Reopen" });
  await waitFor(() => {
    expect(document.activeElement).toBe(reason);
  });
});

test("a status change mounts a new control instead of relabelling the focused one", () => {
  // Company replied and the forward verb share a slot. Unkeyed, React reused
  // the node, so a focused Company replied became Reopen after a refetch and
  // Enter sent a verb nobody chose (review of the merged range, spec 080).
  const queryClient = new QueryClient();
  const view = (status: string) => (
    <QueryClientProvider client={queryClient}>
      <JobActions job={job(1, "Northwind", "80", status) as unknown as Job} />
    </QueryClientProvider>
  );
  const { rerender } = render(view("applied"));
  const replied = screen.getByRole("button", { name: "Company replied" });
  rerender(view("rejected"));
  expect(screen.getByRole("button", { name: "Reopen" })).not.toBe(replied);
});

test("a refusal keeps focus where a browser drops it", async () => {
  // While a write runs its controls are disabled, and a browser moves focus
  // off a disabled control to the page; jsdom does not, so the test does
  // what the browser does. A refused rejection keeps its takeover open, and
  // a refused action from More opens none: either way the refusal is where
  // focus lands (review of the merged range, spec 080).
  for (const [status, open, send] of [
    ["prospect", "Reject", "hybrid"],
    ["prospect", /^More actions/, "Interview invite"],
  ] as const) {
    cleanup();
    let release = (): void => undefined;
    const hold = new Promise<void>((resolve) => {
      release = resolve;
    });
    const refusal = { code: 409, body: { detail: "the tracker declined this" } };
    stubApi({ jobs: [job(1, "Northwind", "80", status)], status: refusal, outcome: refusal, hold });
    const user = userEvent.setup();
    renderPage();

    const row = await rowFor("Northwind");
    await user.click(within(row).getByRole("button", { name: open }));
    await user.click(within(row).getByRole("button", { name: send }));
    (document.activeElement as HTMLElement | null)?.blur();
    release();

    const reason = await screen.findByText("the tracker declined this");
    await waitFor(() => {
      expect(document.activeElement, send).toBe(reason);
    });
  }
});

// --- a refusal is read through the contract (spec 082) ------------------------

type ErrorOut = components["schemas"]["ErrorOut"];

// The JSON body a response declares, or `never` when it declares none.
type Declared<Answer> = Answer extends { content: { "application/json": infer Body } }
  ? Body
  : never;

test("a tracker refusal is read through the contract's types", () => {
  // Checked by `pnpm type-check`, which compiles this file; when the test
  // runs, `expectTypeOf` does nothing. A reader that took `unknown` again,
  // or a tracker write whose 404 or 409 lost its declared body, does not
  // compile.
  expectTypeOf(refusalMessage)
    .parameter(0)
    .toEqualTypeOf<
      | ErrorOut
      | components["schemas"]["DatabaseHeldOut"]
      | components["schemas"]["HTTPValidationError"]
    >();
  expectTypeOf<
    Declared<operations["changeJobStatus"]["responses"][404]>
  >().toEqualTypeOf<ErrorOut>();
  expectTypeOf<
    Declared<operations["changeJobStatus"]["responses"][409]>
  >().toEqualTypeOf<ErrorOut>();
  expectTypeOf<
    Declared<operations["recordCompanyOutcome"]["responses"][404]>
  >().toEqualTypeOf<ErrorOut>();
  expectTypeOf<
    Declared<operations["recordCompanyOutcome"]["responses"][409]>
  >().toEqualTypeOf<ErrorOut>();
  expectTypeOf<Declared<operations["rescoreJob"]["responses"][404]>>().toEqualTypeOf<ErrorOut>();
  expectTypeOf<Declared<operations["rescoreJob"]["responses"][409]>>().toEqualTypeOf<ErrorOut>();
});

test("each tracker write shows its refusal in the API's words", async () => {
  // The reader takes the contract's types now (spec 082), and this holds
  // that what it shows did not change. A 409 from status and from outcome
  // was shown before; a rescore refusal and a 404 from any write were not.
  const writes = [
    { route: "status", rowStatus: "prospect", clicks: ["Shortlist"] },
    { route: "outcome", rowStatus: "applied", clicks: ["Company replied", "ghosted"] },
    { route: "rescore", rowStatus: "prospect", clicks: [/^More actions/, "Rescore"] },
  ] as const;
  for (const { route, rowStatus, clicks } of writes) {
    for (const code of [404, 409]) {
      cleanup();
      const detail = `the tracker refused this ${route} with a ${String(code)}`;
      const options: Parameters<typeof stubApi>[0] = {
        jobs: [job(1, "Northwind", "80", rowStatus)],
      };
      options[route] = { code, body: { detail } };
      stubApi(options);
      const user = userEvent.setup();
      renderPage();

      const row = await rowFor("Northwind");
      for (const name of clicks) {
        await user.click(within(row).getByRole("button", { name }));
      }
      expect(await within(row).findByText(detail), detail).toBeDefined();
    }
  }
});
