import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { OperationsPage } from "./OperationsPage";

/**
 * The Operations page (spec 050, as amended by spec 096).
 *
 * The schedule never reads healthy on a guess, every change is a separate
 * deliberate action, and a run's report is the CLI's own words.
 */

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

type Call = { url: string; method: string; body: unknown; token: string | null };

const NOW = "2026-10-10T08:00:00Z";

function schedule(overrides: Record<string, unknown> = {}) {
  return {
    jobs: [
      {
        name: "discovery",
        cadence: "daily at 09:00, 20:00",
        records: [
          {
            key: "discovery",
            last_success_at: NOW,
            summary: "discovery: last succeeded today",
            overdue: false,
          },
        ],
      },
      {
        name: "digest",
        cadence: "daily at 20:30",
        records: [
          {
            key: "digest",
            last_success_at: "2026-09-30T20:30:00Z",
            summary: "digest: last succeeded 10 days ago",
            overdue: true,
          },
        ],
      },
    ],
    error: null,
    installed_state: "Whether each job is installed and loaded in launchd is the host's to report.",
    host_command: "harrier schedule status",
    ...overrides,
  };
}

function runBody(id: string, kind: string, state = "running") {
  return {
    id,
    kind,
    state,
    created_at: NOW,
    started_at: null,
    ended_at: null,
    exit_code: null,
  };
}

// A multipart body is kept as its text; anything else was JSON.
function parsed(raw: string): unknown {
  try {
    return JSON.parse(raw) as unknown;
  } catch {
    return raw;
  }
}

function stubApi(
  options: {
    schedule?: unknown;
    refuse?: Record<string, { status: number; detail: string }>;
    check?: unknown;
    linked?: unknown;
  } = {},
): Call[] {
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
          body: raw === "" ? null : parsed(raw),
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

      const refusal = options.refuse?.[url.pathname];
      if (refusal !== undefined) return reply(refusal.status, { detail: refusal.detail });
      if (url.pathname === "/api/session") return reply(200, { token: "test-token" });
      if (url.pathname === "/api/ops/schedule") return reply(200, options.schedule ?? schedule());
      if (url.pathname === "/api/ops/feeds") return reply(200, runBody("feeds1", "check-feeds"));
      if (url.pathname === "/api/ops/feeds/prune") {
        return reply(200, runBody("prune1", "check-feeds"));
      }
      if (url.pathname === "/api/ops/reconsider") {
        return reply(200, runBody(`re${String(calls.length)}`, "reconsider"));
      }
      if (url.pathname === "/api/ops/digest") return reply(200, runBody("digest1", "digest"));
      if (url.pathname === "/api/ops/check") {
        return reply(200, options.check ?? { breaches: [], unresolved_links: [] });
      }
      if (url.pathname === "/api/ops/check/link-contacts") {
        return reply(200, options.linked ?? { linked: 0, unmatched: 0 });
      }
      if (url.pathname === "/api/ops/events/backfill") return reply(200, runBody("bf1", "x"));
      if (url.pathname === "/api/ops/evaluate-prospects") {
        return reply(200, runBody(`ev${String(calls.length)}`, "evaluate-prospects"));
      }
      if (url.pathname === "/api/ops/scoring/export") return reply(200, runBody("sx1", "x"));
      if (url.pathname === "/api/ops/discover") return reply(200, runBody("disc1", "discovery"));
      if (url.pathname.startsWith("/api/runs/")) {
        const id = url.pathname.split("/")[3] ?? "";
        return reply(200, runBody(id, "any"));
      }
      return reply(404, { detail: `unstubbed ${url.pathname}` });
    }),
  );
  return calls;
}

class FakeEventSource {
  static all: FakeEventSource[] = [];
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(readonly url: string) {
    FakeEventSource.all.push(this);
  }
  emit(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent<string>);
  }
  close(): void {
    /* nothing to release in a test */
  }
}

function lastSource(): FakeEventSource {
  const source = FakeEventSource.all.at(-1);
  if (source === undefined) throw new Error("no run was streamed");
  return source;
}

function renderPage(): void {
  FakeEventSource.all = [];
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <OperationsPage
        createEventSource={(url) => new FakeEventSource(url) as unknown as EventSource}
      />
    </QueryClientProvider>,
  );
}

function posted(calls: Call[], path: string): Call[] {
  return calls.filter((call) => call.method === "POST" && call.url === `/api${path}`);
}

// --- the schedule -------------------------------------------------------------

test("a job with no recent success reads as overdue and nothing reads as healthy", async () => {
  stubApi();
  renderPage();

  const digest = (await screen.findByText("digest: last succeeded 10 days ago")).closest("li");
  expect(digest?.textContent).toContain("Overdue");
  const discovery = screen.getByText("discovery: last succeeded today").closest("li");
  expect(discovery?.textContent).toContain("Within its cadence");
  expect(discovery?.textContent).not.toContain("Overdue");
  expect(screen.queryByText(/healthy/i)).toBeNull();
});

test("the installed and loaded state is the host's to report, with the command", async () => {
  stubApi();
  renderPage();

  const note = await screen.findByText(/the host's to report/);
  expect(note.textContent).toContain("harrier schedule status");
  // No installed or loaded badge anywhere: the server cannot know either.
  expect(screen.queryByText(/^installed$/i)).toBeNull();
  expect(screen.queryByText(/^loaded$/i)).toBeNull();
});

test("an unreadable schedule definition is shown in the loader's words", async () => {
  stubApi({ schedule: schedule({ jobs: [], error: "config/schedule.json: no jobs" }) });
  renderPage();

  const alert = await screen.findByText(/could not be read/);
  expect(alert.textContent).toContain("config/schedule.json: no jobs");
});

// --- feed health --------------------------------------------------------------

test("checking feeds changes nothing and pruning is a separate confirmed action", async () => {
  const calls = stubApi();
  const user = userEvent.setup();
  renderPage();

  await user.click(await screen.findByRole("button", { name: "Check feeds" }));
  await waitFor(() => {
    expect(posted(calls, "/ops/feeds")).toHaveLength(1);
  });
  act(() => {
    lastSource().emit({ type: "state_change", state: "succeeded", exit_code: 0 });
  });

  await user.click(screen.getByRole("button", { name: "Prune dead boards…" }));
  // Opening the confirmation sends nothing.
  expect(posted(calls, "/ops/feeds/prune")).toHaveLength(0);
  await user.click(screen.getByRole("button", { name: "Prune the dead boards" }));
  await waitFor(() => {
    expect(posted(calls, "/ops/feeds/prune")).toHaveLength(1);
  });
  expect(posted(calls, "/ops/feeds/prune")[0]?.body).toEqual({ confirm: true });
});

test("no boards configured reaches the operator in the CLI's words", async () => {
  stubApi();
  const user = userEvent.setup();
  renderPage();

  await user.click(await screen.findByRole("button", { name: "Check feeds" }));
  await waitFor(() => {
    expect(FakeEventSource.all.length).toBeGreaterThan(0);
  });
  act(() => {
    lastSource().emit({ type: "log_line", line: "no boards configured" });
    lastSource().emit({ type: "state_change", state: "failed", exit_code: 1 });
  });
  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toBe("no boards configured");
});

// --- reconsideration ----------------------------------------------------------

test("reconsideration reports first and offers clearing only after the report", async () => {
  const calls = stubApi();
  const user = userEvent.setup();
  renderPage();

  expect(screen.queryByRole("button", { name: "Clear what the report found" })).toBeNull();
  await user.selectOptions(await screen.findByLabelText("Source"), "greenhouse");
  await user.click(screen.getByRole("button", { name: "Report what would be cleared" }));
  await waitFor(() => {
    expect(posted(calls, "/ops/reconsider")).toHaveLength(1);
  });
  expect(posted(calls, "/ops/reconsider")[0]?.body).toEqual({
    apply: false,
    source: "greenhouse",
  });
  // Not offered while the report is still running.
  expect(screen.queryByRole("button", { name: "Clear what the report found" })).toBeNull();

  act(() => {
    lastSource().emit({ type: "log_line", line: "1 would be cleared; re-run with --apply" });
    lastSource().emit({ type: "state_change", state: "succeeded", exit_code: 0 });
  });
  await user.click(await screen.findByRole("button", { name: "Clear what the report found" }));
  await waitFor(() => {
    expect(posted(calls, "/ops/reconsider")).toHaveLength(2);
  });
  expect(posted(calls, "/ops/reconsider")[1]?.body).toEqual({ apply: true, source: "greenhouse" });
});

test("the report keeps the CLI's words, so the two zero outcomes stay apart", async () => {
  stubApi();
  const user = userEvent.setup();
  renderPage();

  await user.click(await screen.findByRole("button", { name: "Report what would be cleared" }));
  await waitFor(() => {
    expect(FakeEventSource.all.length).toBeGreaterThan(0);
  });
  const perSource =
    "greenhouse: 3 recorded, 0 under older rules, 0 now eligible again, 3 still rejected, 0 left alone because you rejected them";
  act(() => {
    lastSource().emit({ type: "log_line", line: perSource });
    lastSource().emit({ type: "log_line", line: "nothing is eligible to clear" });
    lastSource().emit({ type: "state_change", state: "succeeded", exit_code: 0 });
  });
  const log = await screen.findByLabelText("Reconsideration report log");
  expect(log.textContent).toContain(perSource);
  expect(log.textContent).toContain("nothing is eligible to clear");
});

// --- the digest ---------------------------------------------------------------

test("the preview sends nothing and sending is its own marked button", async () => {
  const calls = stubApi();
  const user = userEvent.setup();
  renderPage();

  await user.click(await screen.findByRole("button", { name: "Preview, send nothing" }));
  await waitFor(() => {
    expect(posted(calls, "/ops/digest")).toHaveLength(1);
  });
  expect(posted(calls, "/ops/digest")[0]?.body).toEqual({
    dry_run: true,
    resend: false,
    date: null,
  });
  expect(screen.getByText(/posts a Telegram message/)).toBeTruthy();
});

test("a day already sent is refused in the server's words and resending is explicit", async () => {
  const calls = stubApi({
    refuse: {
      "/api/ops/digest": {
        status: 409,
        detail:
          "the digest for 2026-10-09 was already sent at 2026-10-09T20:30:00Z; send it again with resend: true",
      },
    },
  });
  const user = userEvent.setup();
  renderPage();

  await user.type(await screen.findByLabelText("Day"), "2026-10-09");
  await user.click(screen.getByRole("button", { name: "Send to Telegram" }));
  expect(await screen.findByText(/was already sent at 2026-10-09T20:30:00Z/)).toBeTruthy();
  expect(posted(calls, "/ops/digest")[0]?.body).toEqual({
    dry_run: false,
    resend: false,
    date: "2026-10-09",
  });

  await user.click(screen.getByRole("button", { name: "Send it again anyway" }));
  await waitFor(() => {
    expect(posted(calls, "/ops/digest")).toHaveLength(2);
  });
  expect(posted(calls, "/ops/digest")[1]?.body).toEqual({
    dry_run: false,
    resend: true,
    date: "2026-10-09",
  });
});

test("a digest produced and not delivered is told apart from one never produced", async () => {
  stubApi();
  const user = userEvent.setup();
  renderPage();

  await user.click(await screen.findByRole("button", { name: "Send to Telegram" }));
  await waitFor(() => {
    expect(FakeEventSource.all.length).toBeGreaterThan(0);
  });
  act(() => {
    lastSource().emit({ type: "progress", step: 1, total: 2, message: "digest produced" });
    lastSource().emit({ type: "log_line", line: "Daily job digest" });
    lastSource().emit({ type: "state_change", state: "failed", exit_code: 1 });
  });
  expect(await screen.findByText(/Produced and not delivered/)).toBeTruthy();

  cleanup();
  stubApi();
  renderPage();
  await user.click(await screen.findByRole("button", { name: "Send to Telegram" }));
  await waitFor(() => {
    expect(FakeEventSource.all.length).toBeGreaterThan(0);
  });
  act(() => {
    lastSource().emit({ type: "log_line", line: "Traceback (most recent call last):" });
    lastSource().emit({ type: "state_change", state: "failed", exit_code: 1 });
  });
  expect(await screen.findByText("No digest was produced.")).toBeTruthy();
});

test("a delivered digest says so", async () => {
  stubApi();
  const user = userEvent.setup();
  renderPage();

  await user.click(await screen.findByRole("button", { name: "Send to Telegram" }));
  await waitFor(() => {
    expect(FakeEventSource.all.length).toBeGreaterThan(0);
  });
  act(() => {
    lastSource().emit({ type: "progress", step: 1, total: 2, message: "digest produced" });
    lastSource().emit({ type: "progress", step: 2, total: 2, message: "digest delivered" });
    lastSource().emit({ type: "state_change", state: "succeeded", exit_code: 0 });
  });
  expect(await screen.findByText("Delivered to Telegram.")).toBeTruthy();
});

// --- the token ----------------------------------------------------------------

test("every operations write carries the token and no read does", async () => {
  const calls = stubApi();
  const user = userEvent.setup();
  renderPage();

  await screen.findByText("discovery: last succeeded today");
  await user.click(screen.getByRole("button", { name: "Check feeds" }));
  await waitFor(() => {
    expect(posted(calls, "/ops/feeds")).toHaveLength(1);
  });
  act(() => {
    lastSource().emit({ type: "state_change", state: "succeeded", exit_code: 0 });
  });
  await user.click(screen.getByRole("button", { name: "Preview, send nothing" }));
  await waitFor(() => {
    expect(posted(calls, "/ops/digest")).toHaveLength(1);
  });

  const ops = calls.filter((call) => call.url.startsWith("/api/ops/"));
  for (const call of ops) {
    // The data check names contacts, so it is the one read with the token.
    if (call.method === "GET" && call.url !== "/api/ops/check") expect(call.token).toBeNull();
    else expect(call.token).toBe("test-token");
  }
  expect(ops.some((call) => call.method === "GET")).toBe(true);
});

// --- spec 095: data checks, decision history, evaluation, export, discovery ---

test("data checks list each finding in the domain's words", async () => {
  stubApi({
    check: {
      breaches: [{ job_id: "4", breach: "applied with no applied date" }],
      unresolved_links: [
        { contact: "Avery Invented", breach: "a linked job that matches no tracked job" },
      ],
    },
  });
  renderPage();

  expect(await screen.findByText("job 4: applied with no applied date")).toBeTruthy();
  expect(
    screen.getByText("contact Avery Invented: a linked job that matches no tracked job"),
  ).toBeTruthy();
  expect(screen.queryByText("Nothing to report.")).toBeNull();
});

test("a clean data check says there is nothing to report", async () => {
  stubApi();
  renderPage();

  expect(await screen.findByText("Nothing to report.")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Link contact ids…" })).toBeNull();
});

test("linking contact ids is a separate confirmed action that names the count", async () => {
  const calls = stubApi({
    check: {
      breaches: [],
      unresolved_links: [
        { contact: "Avery Invented", breach: "a linked job that matches no tracked job" },
        { contact: "Blake Invented", breach: "a linked job that matches no tracked job" },
      ],
    },
    linked: { linked: 1, unmatched: 1 },
  });
  const user = userEvent.setup();
  renderPage();

  await user.click(await screen.findByRole("button", { name: "Link contact ids…" }));
  expect(posted(calls, "/ops/check/link-contacts")).toHaveLength(0);
  const confirm = screen.getByRole("group", { name: "Confirm linking" });
  expect(confirm.textContent).toContain("2 contact links resolve nowhere now");
  await user.click(screen.getByRole("button", { name: "Link them" }));
  expect(await screen.findByText("Linked 1; 1 left unmatched.")).toBeTruthy();
  expect(posted(calls, "/ops/check/link-contacts")[0]?.body).toEqual({ confirm: true });
});

test("a dry backfill comes first and writing names the count it will write", async () => {
  const calls = stubApi();
  const user = userEvent.setup();
  renderPage();

  await user.click(
    await screen.findByRole("button", { name: "Count what a backfill would write" }),
  );
  await waitFor(() => {
    expect(posted(calls, "/ops/events/backfill")).toHaveLength(1);
  });
  expect(posted(calls, "/ops/events/backfill")[0]?.body).toEqual({ dry_run: true });
  // Not offered while the count is still running.
  expect(screen.queryByRole("button", { name: /reconstructed events$/ })).toBeNull();
  act(() => {
    lastSource().emit({ type: "log_line", line: "would write 7 events" });
    lastSource().emit({ type: "state_change", state: "succeeded", exit_code: 0 });
  });
  await user.click(await screen.findByRole("button", { name: "Write the 7 reconstructed events" }));
  await waitFor(() => {
    expect(posted(calls, "/ops/events/backfill")).toHaveLength(2);
  });
  expect(posted(calls, "/ops/events/backfill")[1]?.body).toEqual({ dry_run: false });
});

test("batch evaluation offers rejection only after a report, naming the count", async () => {
  const calls = stubApi();
  const user = userEvent.setup();
  renderPage();

  await user.type(await screen.findByLabelText("Confidence to reject, 0 to 1"), "0.9");
  await user.click(screen.getByRole("button", { name: "Evaluate, reject nothing" }));
  await waitFor(() => {
    expect(posted(calls, "/ops/evaluate-prospects")).toHaveLength(1);
  });
  expect(posted(calls, "/ops/evaluate-prospects")[0]?.body).toEqual({
    apply: false,
    threshold: 0.9,
    limit: null,
    refresh: false,
    include_borderline: false,
  });
  expect(screen.queryByRole("button", { name: /^Reject the/ })).toBeNull();
  act(() => {
    lastSource().emit({ type: "log_line", line: "would_reject=3" });
    lastSource().emit({ type: "state_change", state: "succeeded", exit_code: 0 });
  });
  await user.click(
    await screen.findByRole("button", { name: "Reject the 3 prospects as the system's decision" }),
  );
  await waitFor(() => {
    expect(posted(calls, "/ops/evaluate-prospects")).toHaveLength(2);
  });
  expect(posted(calls, "/ops/evaluate-prospects")[1]?.body).toMatchObject({
    apply: true,
    threshold: 0.9,
  });
});

test("the feature export runs here and training is said to stay on the host", async () => {
  const calls = stubApi();
  const user = userEvent.setup();
  renderPage();

  expect(await screen.findByText(/Training runs on the host/)).toBeTruthy();
  await user.click(screen.getByRole("button", { name: "Write the feature export" }));
  await waitFor(() => {
    expect(posted(calls, "/ops/scoring/export")).toHaveLength(1);
  });
});

test("discovery options and an upload are sent as multipart and stream on the runs panel", async () => {
  const calls = stubApi();
  // jsdom serialises a FormData request body as "[object FormData]", so the
  // fields are read as the page appends them.
  const appended: [string, unknown][] = [];
  const append = vi.spyOn(FormData.prototype, "append").mockImplementation((name, value) => {
    appended.push([name, value]);
  });
  const user = userEvent.setup();
  renderPage();

  await user.click(await screen.findByLabelText(/^Dry run, write nothing/));
  await user.click(screen.getByLabelText("Send the Telegram summary"));
  await user.selectOptions(screen.getByLabelText("Only one source"), "greenhouse");
  const file = new File(['[{"title": "Invented Role"}]'], "export.json", {
    type: "application/json",
  });
  await user.upload(screen.getByLabelText("Apify dataset export"), file);
  await user.click(screen.getByRole("button", { name: "Run discovery with these options" }));
  await waitFor(() => {
    expect(posted(calls, "/ops/discover")).toHaveLength(1);
  });
  append.mockRestore();
  const fields = new Map(appended);
  expect(fields.get("dry_run")).toBe("true");
  expect(fields.get("notify")).toBe("false");
  expect(fields.get("only_source")).toBe("greenhouse");
  expect(fields.get("dataset_file")).toBe(file);
  // Left empty, the count is the configured one, decided by the server.
  expect(fields.has("apify_count")).toBe(false);
  expect(fields.has("wellfound_file")).toBe(false);
  // The run is the Runs panel's: it shows there, and its buttons wait.
  expect(await screen.findByText(/run disc1/)).toBeTruthy();
  expect(screen.getByRole("button", { name: "Run discovery" })).toHaveProperty("disabled", true);
});
