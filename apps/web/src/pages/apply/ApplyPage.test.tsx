import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import type { Job } from "../../entities/job";
import { INDUSTRY_TRACK } from "../../shared/track/fixtures";
import { installRunStreams } from "../../shared/testing/runStreams";
import type { RunStreams } from "../../shared/testing/runStreams";

import { ApplyPage } from "./ApplyPage";

/**
 * The Apply page starts the same operations the CLI does (spec 047).
 *
 * Two properties matter here and neither is visible from the server side.
 * The first is that a refusal reaches the operator in the words the API or
 * the gate used, rather than as a spinner that stops. The second is the token
 * asymmetry: artifact reads carry it and tracker reads do not, which is a
 * rule that lives in the client and can only be checked from here.
 */

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const JOB: Job = {
  id: 7,
  company: "Northwind Labs",
  title: "Senior Frontend Engineer",
  url: "https://boards.example.com/northwind/1",
  location: "Remote, Europe",
  source: "greenhouse",
  status: "shortlisted",
  score: "80",
  fit_score: "",
  next_action: "",
  added_at: "2026-01-01",
} as unknown as Job;

type Call = { url: string; method: string; body: unknown; token: string | null };

function artifact(kind: string, exists: boolean, producedBy: string) {
  return {
    kind,
    exists,
    produced_by: producedBy,
    media_type: "text/markdown; charset=utf-8",
    filename: `${kind}.md`,
  };
}

function stubApi(options: {
  start?: { code: number; body: unknown };
  artifacts?: unknown[];
  /** What GET /runs/{id} reports, which a real server would keep in step
   * with the state_change on the stream. */
  runState?: string;
  /** The stored description (spec 149); stored by default, as discovery
   * leaves most jobs. */
  description?: { text: string; stored: boolean };
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
      if (url.pathname.endsWith("/artifacts")) {
        return reply(200, options.artifacts ?? []);
      }
      // No brief stored, as the API answers it (spec 095).
      if (url.pathname.endsWith("/brief")) return reply(404, { detail: "no brief" });
      if (url.pathname.endsWith("/description")) {
        return reply(
          200,
          options.description ?? { text: "An invented posting for a remote role.", stored: true },
        );
      }
      if (url.pathname.startsWith("/api/apply/")) {
        const answer = options.start ?? {
          code: 200,
          body: {
            id: "run123",
            kind: "tailor",
            state: "running",
            created_at: "2026-01-01T00:00:00Z",
            started_at: null,
            ended_at: null,
            exit_code: null,
          },
        };
        return reply(answer.code, answer.body);
      }
      if (url.pathname.startsWith("/api/runs/")) {
        return reply(200, {
          id: "run123",
          kind: "tailor",
          state: options.runState ?? "running",
          created_at: "2026-01-01T00:00:00Z",
          started_at: null,
          ended_at: null,
          exit_code: null,
        });
      }
      return reply(404, { detail: `unstubbed ${url.pathname}` });
    }),
  );
  return calls;
}

let streams: RunStreams;

function renderPage(): void {
  streams = installRunStreams();
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <ApplyPage job={JOB} track={INDUSTRY_TRACK} onBack={() => undefined} />
    </QueryClientProvider>,
  );
}

test("a half-pasted description does not carry over to another job's page", async () => {
  stubApi({});
  installRunStreams();
  const user = userEvent.setup();
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const page = (job: Job) => (
    <QueryClientProvider client={queryClient}>
      <ApplyPage job={job} track={INDUSTRY_TRACK} onBack={() => undefined} />
    </QueryClientProvider>
  );
  const { rerender } = render(page(JOB));

  await user.click(await screen.findByRole("button", { name: "Replace" }));
  await user.paste(" and a half-pasted line");
  rerender(page({ ...JOB, id: 8 }));

  // Job 8 shows its own stored text, not job 7's open draft.
  expect(await screen.findByRole("button", { name: "Replace" })).toBeTruthy();
  expect(screen.queryByLabelText("Paste the job description")).toBeNull();
});

// --- starting an operation ---------------------------------------------------

test("tailoring posts to the resume route for this job", async () => {
  const calls = stubApi({});
  const user = userEvent.setup();
  renderPage();

  await user.click(screen.getByRole("button", { name: "Run tailoring" }));

  await waitFor(() => {
    expect(calls.some((call) => call.url === "/api/apply/7/resume")).toBe(true);
  });
  const sent = calls.find((call) => call.url === "/api/apply/7/resume");
  expect(sent?.method).toBe("POST");
  // No description in the body: the run reads the stored one (spec 149).
  expect(sent?.body).toEqual({ no_ai: false });
});

test("the resume and evaluate operations ask for no description", async () => {
  stubApi({});
  const user = userEvent.setup();
  renderPage();

  // The panel holds the description, once, for every operation (spec 149).
  expect(await screen.findByLabelText("Stored description")).toBeTruthy();
  expect(screen.queryByLabelText("Job description (optional)")).toBeNull();
  await user.click(screen.getByRole("radio", { name: "Evaluate offer" }));
  expect(screen.queryByLabelText("Job description (optional)")).toBeNull();
});

test("tailoring a job with no description is refused in the API's words", async () => {
  stubApi({
    description: { text: "", stored: false },
    start: {
      code: 409,
      body: { detail: "no job description is stored for job 7; paste one on the Apply page" },
    },
  });
  const user = userEvent.setup();
  renderPage();

  expect(
    await screen.findByText(
      "No description was captured for this job. Paste it to use it for every operation.",
    ),
  ).toBeTruthy();
  await user.click(screen.getByRole("button", { name: "Run tailoring" }));

  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toBe(
    "no job description is stored for job 7; paste one on the Apply page",
  );
});

test("each operation posts to its own route", async () => {
  const calls = stubApi({});
  const user = userEvent.setup();
  renderPage();

  await user.click(screen.getByRole("radio", { name: "Evaluate offer" }));
  await user.click(screen.getByRole("button", { name: "Evaluate the offer" }));

  await waitFor(() => {
    expect(calls.some((call) => call.url === "/api/apply/7/evaluate")).toBe(true);
  });
  expect(calls.some((call) => call.url === "/api/apply/7/resume")).toBe(false);
});

test("drafting answers will not start without a question", async () => {
  stubApi({});
  const user = userEvent.setup();
  renderPage();

  await user.click(screen.getByRole("radio", { name: "Draft answers" }));
  expect(screen.getByRole("button", { name: "Draft the answers" }).hasAttribute("disabled")).toBe(
    true,
  );
  expect(screen.getByText("Add at least one question to draft.")).toBeTruthy();
});

// --- refusals reach the operator ---------------------------------------------

test("a refusal is shown in the words the API used", async () => {
  const calls = stubApi({
    start: {
      code: 409,
      body: { detail: "resume render validation failed: the PDF is four pages" },
    },
  });
  const user = userEvent.setup();
  renderPage();

  await user.click(screen.getByRole("button", { name: "Run tailoring" }));

  const alert = await screen.findByRole("alert");
  // The gate's own sentence, not "something went wrong".
  expect(alert.textContent).toContain("resume render validation failed");
  expect(calls.some((call) => call.url === "/api/apply/7/resume")).toBe(true);
});

test("a gate's refusal on a failed run is shown, not swallowed", async () => {
  stubApi({ runState: "failed" });
  const user = userEvent.setup();
  renderPage();

  await user.click(screen.getByRole("button", { name: "Run tailoring" }));
  await waitFor(() => {
    expect(streams.connections.length).toBeGreaterThan(0);
  });

  // This is how a truth-gate or PDF-gate refusal actually arrives: not as a
  // status code on the start request, but as the last line a failed run
  // printed. Swallowing it would leave the operator with "failed" and no
  // reason (spec 047).
  const source = streams.last;
  await act(async () => {
    source.emit({
      type: "log_line",
      line: "tailor failed: resume claims an unverified achievement",
    });
    source.emit({ type: "state_change", state: "failed", exit_code: 1 });
    await Promise.resolve();
  });

  const alerts = await screen.findAllByRole("alert");
  expect(alerts.some((node) => node.textContent.includes("unverified achievement"))).toBe(true);
});

test("a lost stream is told in a status region that was already there", async () => {
  stubApi({});
  const user = userEvent.setup();
  renderPage();

  // The page's regions before the run starts. A region inserted with its
  // text may never be spoken (spec 156).
  const before = screen.getAllByRole("status");
  await user.click(screen.getByRole("button", { name: "Run tailoring" }));
  await waitFor(() => {
    expect(streams.connections.length).toBeGreaterThan(0);
  });
  act(() => {
    streams.last.fail();
  });

  const notice = await screen.findByText(/^Lost the log stream/);
  expect(before.some((region) => region.contains(notice))).toBe(true);
  // And no region came with it. A region that mounted since (a panel that
  // finished loading) must be empty; one holding the message would be the
  // insertion spec 156 forbids.
  const added = screen.getAllByRole("status").filter((region) => !before.includes(region));
  expect(added.filter((region) => region.textContent !== "")).toEqual([]);
});

// --- artifacts ---------------------------------------------------------------

test("an absent artifact is listed with the operation that would produce it", async () => {
  // Listed rather than omitted: an omitted row cannot say what would make it.
  stubApi({
    artifacts: [
      artifact("resume-pdf", false, "resume"),
      artifact("cover-letter-pdf", false, "cover-letter"),
    ],
  });
  renderPage();

  expect(await screen.findByText("Tailored resume")).toBeTruthy();
  expect(
    screen.getByRole("button", { name: /^Select Tailor resume to produce Tailored resume/ }),
  ).toBeTruthy();
  expect(
    screen.getByRole("button", { name: /^Select Draft cover letter to produce Cover letter/ }),
  ).toBeTruthy();
});

test("the button on an absent artifact selects its operation and does not run it", async () => {
  // These operations spend money. A control in a list of files that quietly
  // started one would be a surprise, so it selects and the operator runs it.
  const calls = stubApi({ artifacts: [artifact("resume-pdf", false, "resume")] });
  const user = userEvent.setup();
  renderPage();

  await user.click(await screen.findByRole("button", { name: /^Select Tailor resume to produce/ }));

  expect(screen.getByRole("radio", { name: "Tailor resume" }).getAttribute("aria-checked")).toBe(
    "true",
  );
  expect(calls.some((call) => call.method === "POST")).toBe(false);
});

test("a present artifact is offered to open", async () => {
  stubApi({ artifacts: [artifact("answers", true, "answers")] });
  renderPage();

  expect(await screen.findByText("Application answers")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Open Application answers" })).toBeTruthy();
});

test("the artifact summary counts what exists", async () => {
  stubApi({
    artifacts: [artifact("answers", true, "answers"), artifact("resume-pdf", false, "resume")],
  });
  renderPage();

  expect(await screen.findByText("1 of 2 generated")).toBeTruthy();
});

test("nothing generated yet reads as such rather than as an empty list", async () => {
  stubApi({ artifacts: [artifact("answers", false, "answers")] });
  renderPage();

  expect(await screen.findByText("Nothing generated for this job yet.")).toBeTruthy();
});

// --- the token asymmetry lives in the client ---------------------------------

test("the artifact read carries the token although other reads do not", async () => {
  const calls = stubApi({ artifacts: [artifact("answers", true, "answers")] });
  const user = userEvent.setup();
  renderPage();

  // Start something, so the page also issues a plain read (the run poll) to
  // compare against. Asserting on /api/session would not work: the client
  // caches the token for the module's lifetime, so a later test never
  // re-fetches it.
  await user.click(screen.getByRole("button", { name: "Run tailoring" }));

  await waitFor(() => {
    expect(calls.some((call) => call.url === "/api/apply/7/artifacts")).toBe(true);
  });
  await waitFor(() => {
    expect(calls.some((call) => call.url.startsWith("/api/runs/"))).toBe(true);
  });

  // The rule this pins: artifacts are the densest personal content the system
  // holds, so their read is the one GET that authenticates (spec 047).
  const artifacts = calls.find((call) => call.url === "/api/apply/7/artifacts");
  expect(artifacts?.method).toBe("GET");
  expect(artifacts?.token).toBe("test-token");

  const run = calls.find((call) => call.url.startsWith("/api/runs/") && call.method === "GET");
  expect(run?.token).toBeNull();
});
