import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

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
  add?: { code: number; body: unknown };
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
      if (url.pathname === "/api/jobs") return reply(200, options.jobs ?? []);
      if (url.pathname === "/api/tracker/queue") return reply(200, options.queue ?? []);
      if (url.pathname.endsWith("/status")) {
        const answer = options.status ?? { code: 200, body: job(1, "Northwind", "80") };
        return reply(answer.code, answer.body);
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
  // visible at once.
  stubApi({ jobs: [job(1, "Northwind", "80")] });
  const user = userEvent.setup();
  renderPage();
  const row = await rowFor("Northwind");

  await user.click(within(row).getByRole("button", { name: /^More actions/ }));
  for (const label of ["Shortlist", "Request CV", "Applied", "Interviewing", "Reject", "Rescore"]) {
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

  await waitFor(() => {
    const sent = calls.find((call) => call.url === "/api/tracker/1/status");
    expect(sent?.body).toEqual({ verb: "reject", reason: "wrong stack" });
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
    expect(sent?.body).toEqual({ verb: "reject", reason: "hybrid" });
  });
});

test("every pill submits its exact lowercase label as the reason", async () => {
  // The strings are the stored values; consistent spellings are what makes
  // rejection_reason groupable later (spec 056).
  for (const why of ["hybrid", "onsite", "closed", "missing stack"]) {
    cleanup();
    const calls = stubApi({ jobs: [job(1, "Northwind", "80")] });
    const user = userEvent.setup();
    renderPage();

    const row = await rowFor("Northwind");
    await user.click(within(row).getByRole("button", { name: "Reject" }));
    await user.click(within(row).getByRole("button", { name: why }));

    await waitFor(() => {
      const sent = calls.find((call) => call.url === "/api/tracker/1/status");
      expect(sent?.body, `${why} did not travel verbatim`).toEqual({
        verb: "reject",
        reason: why,
      });
    });
  }
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
