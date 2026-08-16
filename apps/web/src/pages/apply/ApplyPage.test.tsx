import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import type { Job } from "../../entities/job";

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

class FakeEventSource {
  static last: FakeEventSource | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor() {
    FakeEventSource.last = this;
  }
  emit(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent<string>);
  }
  close(): void {
    /* nothing to release in a test */
  }
}

function renderPage(): void {
  FakeEventSource.last = null;
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <ApplyPage
        job={JOB}
        onBack={() => undefined}
        createEventSource={() => new FakeEventSource() as unknown as EventSource}
      />
    </QueryClientProvider>,
  );
}

// --- starting an operation ---------------------------------------------------

test("tailoring posts to the resume route for this job", async () => {
  const calls = stubApi({});
  const user = userEvent.setup();
  renderPage();

  await user.click(screen.getByRole("button", { name: "Tailor resume" }));

  await waitFor(() => {
    expect(calls.some((call) => call.url === "/api/apply/7/resume")).toBe(true);
  });
  const sent = calls.find((call) => call.url === "/api/apply/7/resume");
  expect(sent?.method).toBe("POST");
  expect(sent?.body).toEqual({ jd_text: "", no_ai: false });
});

test("each operation posts to its own route", async () => {
  const calls = stubApi({});
  const user = userEvent.setup();
  renderPage();

  await user.click(screen.getByRole("radio", { name: "Evaluate offer" }));
  await user.click(screen.getByRole("button", { name: "Evaluate offer" }));

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
  expect(screen.getByRole("button", { name: "Draft answers" }).hasAttribute("disabled")).toBe(true);
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

  await user.click(screen.getByRole("button", { name: "Tailor resume" }));

  const alert = await screen.findByRole("alert");
  // The gate's own sentence, not "something went wrong".
  expect(alert.textContent).toContain("resume render validation failed");
  expect(calls.some((call) => call.url === "/api/apply/7/resume")).toBe(true);
});

test("a gate's refusal on a failed run is shown, not swallowed", async () => {
  stubApi({ runState: "failed" });
  const user = userEvent.setup();
  renderPage();

  await user.click(screen.getByRole("button", { name: "Tailor resume" }));
  await waitFor(() => {
    expect(FakeEventSource.last).not.toBeNull();
  });

  // This is how a truth-gate or PDF-gate refusal actually arrives: not as a
  // status code on the start request, but as the last line a failed run
  // printed. Swallowing it would leave the operator with "failed" and no
  // reason (spec 047).
  const source = FakeEventSource.last;
  await act(async () => {
    source?.emit({
      type: "log_line",
      line: "tailor failed: resume claims an unverified achievement",
    });
    source?.emit({ type: "state_change", state: "failed", exit_code: 1 });
    await Promise.resolve();
  });

  const alerts = await screen.findAllByRole("alert");
  expect(alerts.some((node) => node.textContent.includes("unverified achievement"))).toBe(true);
});

// --- artifacts ---------------------------------------------------------------

test("an absent artifact says which operation would produce it", async () => {
  stubApi({
    artifacts: [
      artifact("resume-pdf", false, "resume"),
      artifact("cover-letter-pdf", false, "cover-letter"),
    ],
  });
  renderPage();

  const absent = await screen.findByText(/Not generated yet/);
  expect(absent.textContent).toContain("Tailor resume");
  expect(absent.textContent).toContain("Draft cover letter");
});

test("a present artifact is offered to open", async () => {
  stubApi({ artifacts: [artifact("answers", true, "answers")] });
  renderPage();

  expect(await screen.findByText("Application answers")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Open" })).toBeTruthy();
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
  await user.click(screen.getByRole("button", { name: "Tailor resume" }));

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
