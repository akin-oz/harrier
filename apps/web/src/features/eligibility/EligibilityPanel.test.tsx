import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { Eligibility, EligibilityEntry, Job } from "../../entities/job";
import { INDUSTRY_TRACK } from "../../shared/track/fixtures";
import type { Track } from "../../shared/track";
import { EligibilityPanel } from "./EligibilityPanel";

/**
 * A row's Türkiye eligibility in the browser (spec 123): read on demand,
 * evidence linked only when it is a web address, and the candidate's verdict
 * sent as typed, with the server's refusals shown in its own words.
 */

beforeEach(() => {
  window.history.replaceState(null, "", "/");
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function eligibility(overrides: Partial<Eligibility> = {}): Eligibility {
  return {
    verdict: "unknown",
    routes: [],
    origin: "none",
    evidence_url: "",
    excerpt: "",
    field: "",
    checked_at: "",
    version: "",
    description_sha256: "",
    disagrees: false,
    ...overrides,
  };
}

// An invented company (ADR-008).
function job(overrides: Partial<Eligibility> = {}): Job {
  return {
    id: 7,
    company: "Northwind Labs",
    title: "Senior Frontend Engineer",
    location: "Remote, Europe",
    url: "https://jobs.example.com/northwind/7",
    source: "manual",
    added_at: "2026-10-01",
    fit_score: "",
    status: "prospect",
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
    track: "job",
    deadline: "",
    deadline_passed: false,
    eligibility: eligibility(overrides),
  };
}

function entry(overrides: Partial<EligibilityEntry> = {}): EligibilityEntry {
  return {
    verdict: "excluded",
    routes: [],
    origin: "rule",
    evidence_url: "https://jobs.example.com/northwind/7",
    excerpt: "You must be based in the UK.",
    field: "description",
    matched: "restriction:must be based in the uk",
    checked_at: "2026-10-11T09:00:00Z",
    version: "abc123def456",
    description_sha256: "0".repeat(64),
    at: "2026-10-11T09:00:00Z",
    ...overrides,
  };
}

type Call = { url: string; method: string; body: unknown; token: string | null };

function stubApi(options: {
  history?: EligibilityEntry[];
  historyStatus?: number;
  set?: { code: number; body: unknown };
}): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(String(input), init);
      const url = new URL(request.url);
      const reply = async (code: number, body: unknown): Promise<Response> => {
        const raw = request.method === "GET" ? "" : await request.clone().text();
        calls.push({
          url: url.pathname + url.search,
          method: request.method,
          body: raw === "" ? null : JSON.parse(raw),
          token: request.headers.get("X-Harrier-Token"),
        });
        return new Response(JSON.stringify(body), {
          status: code,
          headers: { "Content-Type": "application/json" },
        });
      };
      if (url.pathname === "/api/session") return reply(200, { token: "test-token" });
      if (url.pathname === "/api/tracker/7/eligibility" && request.method === "GET") {
        return reply(options.historyStatus ?? 200, options.history ?? []);
      }
      if (url.pathname === "/api/tracker/7/eligibility" && request.method === "POST") {
        const answer = options.set ?? { code: 200, body: eligibility({ origin: "candidate" }) };
        return reply(answer.code, answer.body);
      }
      return reply(404, { detail: `unstubbed ${url.pathname}` });
    }),
  );
  return calls;
}

function renderPanel(row: Job, track: Track = INDUSTRY_TRACK): QueryClient {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <EligibilityPanel job={row} track={track} />
    </QueryClientProvider>,
  );
  return queryClient;
}

function reads(calls: Call[]): Call[] {
  return calls.filter((call) => call.method === "GET" && call.url.includes("/eligibility"));
}

test("the panel reads history on open", async () => {
  const calls = stubApi({ history: [entry()] });
  const user = userEvent.setup();
  renderPanel(job());

  expect(reads(calls)).toHaveLength(0);
  await user.click(screen.getByRole("button", { name: "Eligibility" }));
  const list = await screen.findByRole("list", { name: "Eligibility history, newest first" });
  expect(within(list).getAllByRole("listitem")[0]?.textContent).toContain("Türkiye excluded");
  expect(reads(calls)).toHaveLength(1);
  // A read, without the token, on the default track (spec 095).
  expect(reads(calls)[0]?.url).toBe("/api/tracker/7/eligibility");
  expect(reads(calls)[0]?.token).toBeNull();
});

test("the panel links only http evidence", async () => {
  stubApi({});
  const user = userEvent.setup();
  renderPanel(
    job({
      verdict: "excluded",
      origin: "rule",
      evidence_url: "https://jobs.example.com/northwind/7",
      excerpt: "You must be based in the UK.",
      field: "description",
      checked_at: "2026-10-11T09:00:00Z",
      version: "abc123def456",
    }),
  );
  await user.click(screen.getByRole("button", { name: "Eligibility" }));
  const region = screen.getByRole("region");
  expect(region.textContent).toContain("You must be based in the UK.");
  expect(region.textContent).toContain("in the description");
  expect(region.textContent).toContain("checked 2026-10-11 09:00");
  expect(region.textContent).toContain("rules version abc123def456");
  const link = within(region).getByRole("link", { name: "https://jobs.example.com/northwind/7" });
  expect(link.getAttribute("rel")).toBe("noreferrer");
  expect(link.getAttribute("target")).toBe("_blank");
  cleanup();

  stubApi({});
  renderPanel(job({ verdict: "possible", origin: "candidate", evidence_url: "javascript:x()" }));
  await user.click(screen.getByRole("button", { name: "Eligibility" }));
  const other = screen.getByRole("region");
  expect(other.textContent).toContain("javascript:x()");
  expect(within(other).queryByRole("link")).toBeNull();
});

test("the panel explains a disagreement", async () => {
  stubApi({ history: [entry(), entry({ origin: "candidate", verdict: "confirmed" })] });
  const user = userEvent.setup();
  renderPanel(job({ verdict: "confirmed", origin: "candidate", disagrees: true }));
  await user.click(screen.getByRole("button", { name: "Eligibility" }));
  const note = await screen.findByRole("note");
  await waitFor(() => {
    expect(note.textContent).toContain("The rules say: Türkiye excluded.");
  });
  expect(note.textContent).toContain("Yours stays current.");
});

test("the form sends what was entered and refreshes", async () => {
  const calls = stubApi({});
  const user = userEvent.setup();
  const queryClient = renderPanel(job());
  const invalidate = vi.spyOn(queryClient, "invalidateQueries");
  await user.click(screen.getByRole("button", { name: "Eligibility" }));

  const form = screen.getByRole("form", { name: "Record your verdict" });
  const submit = within(form).getByRole("button", { name: "Record verdict" });
  expect((submit as HTMLButtonElement).disabled).toBe(true);
  await user.click(within(form).getByRole("radio", { name: "Türkiye possible" }));
  await user.click(within(form).getByRole("checkbox", { name: "employer of record" }));
  await user.click(within(form).getByRole("checkbox", { name: "contractor or B2B" }));
  await user.type(within(form).getByLabelText("Evidence URL"), "https://careers.example.com/x");
  await user.type(within(form).getByLabelText("Excerpt"), "We hire contractors anywhere.");
  await user.click(submit);

  await waitFor(() => {
    expect(calls.some((call) => call.method === "POST")).toBe(true);
  });
  const sent = calls.find((call) => call.method === "POST");
  // Routes in the fixed order, and no checked_at when the date is empty.
  expect(sent?.body).toEqual({
    verdict: "possible",
    routes: ["contractor", "eor"],
    evidence_url: "https://careers.example.com/x",
    excerpt: "We hire contractors anywhere.",
  });
  expect(sent?.token).toBe("test-token");
  await waitFor(() => {
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["", "jobs"] });
  });
  // Cleared for the next verdict.
  expect(within(form).getByLabelText<HTMLTextAreaElement>("Excerpt").value).toBe("");
});

test("the form sends a checked date when one is set", async () => {
  const calls = stubApi({});
  const user = userEvent.setup();
  renderPanel(job());
  await user.click(screen.getByRole("button", { name: "Eligibility" }));
  const form = screen.getByRole("form", { name: "Record your verdict" });
  await user.click(within(form).getByRole("radio", { name: "Türkiye unknown" }));
  await user.type(within(form).getByLabelText("Checked on (optional)"), "2026-10-10");
  await user.click(within(form).getByRole("button", { name: "Record verdict" }));
  await waitFor(() => {
    expect(calls.find((call) => call.method === "POST")?.body).toMatchObject({
      checked_at: "2026-10-10",
    });
  });
});

test("the form shows refusals and keeps input", async () => {
  stubApi({
    set: {
      code: 409,
      body: {
        detail:
          "a confirmed verdict needs its evidence: the URL it was read at and the words that decide it",
      },
    },
  });
  const user = userEvent.setup();
  renderPanel(job());
  await user.click(screen.getByRole("button", { name: "Eligibility" }));
  const form = screen.getByRole("form", { name: "Record your verdict" });
  await user.click(within(form).getByRole("radio", { name: "Türkiye confirmed" }));
  await user.type(within(form).getByLabelText("Excerpt"), "Hiring in Türkiye.");
  await user.click(within(form).getByRole("button", { name: "Record verdict" }));

  const alert = await within(form).findByRole("alert");
  expect(alert.textContent).toBe(
    "a confirmed verdict needs its evidence: the URL it was read at and the words that decide it",
  );
  // What was typed is kept.
  expect(within(form).getByLabelText<HTMLTextAreaElement>("Excerpt").value).toBe(
    "Hiring in Türkiye.",
  );
  expect(
    within(form).getByRole<HTMLInputElement>("radio", { name: "Türkiye confirmed" }).checked,
  ).toBe(true);
});

test("a refused token shows the API's sentence", async () => {
  // The API's own 403 body (harrier_api.localauth.require_token).
  const sentence =
    "this request did not present the local API token. " +
    "The UI sends it automatically; a page that is not the harrier UI cannot.";
  stubApi({ set: { code: 403, body: { detail: sentence } } });
  const user = userEvent.setup();
  renderPanel(job());
  await user.click(screen.getByRole("button", { name: "Eligibility" }));
  const form = screen.getByRole("form", { name: "Record your verdict" });
  await user.click(within(form).getByRole("radio", { name: "Türkiye possible" }));
  await user.click(within(form).getByRole("button", { name: "Record verdict" }));
  expect((await within(form).findByRole("alert")).textContent).toBe(sentence);
});

test("a refused history read shows the error and can be retried", async () => {
  const calls = stubApi({ historyStatus: 503 });
  const user = userEvent.setup();
  renderPanel(job({ verdict: "possible", origin: "rule" }));
  await user.click(screen.getByRole("button", { name: "Eligibility" }));
  const alert = await screen.findByRole("alert");
  // The row's own verdict still shows.
  expect(screen.getByRole("region").textContent).toContain("Türkiye possible");
  await user.click(within(alert).getByRole("button", { name: "Retry" }));
  await waitFor(() => {
    expect(reads(calls)).toHaveLength(2);
  });
});

test("the form is absent when the track is read only", async () => {
  stubApi({});
  const user = userEvent.setup();
  renderPanel(job(), { ...INDUSTRY_TRACK, archived: true });
  await user.click(screen.getByRole("button", { name: "Eligibility" }));
  expect(screen.queryByRole("form", { name: "Record your verdict" })).toBeNull();
});
