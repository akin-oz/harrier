import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { InboxPage } from "./InboxPage";

/**
 * The Inbox page over the archived events (spec 049).
 *
 * The three states that produce an empty table are the point of this file.
 * A watch that never ran, a watch that ran and classified nothing, and an
 * archive at its rotation cap are different situations needing different
 * things from the operator, and an empty table that reads as any of the
 * others is the defect this page exists to avoid.
 */

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

type Call = { url: string; method: string; body: unknown; token: string | null };

// An invented company at an invented domain (ADR-008). The archive holds no
// subject and no sender, so there is none to invent here either.
function event(overrides: Record<string, unknown> = {}) {
  return {
    kind: "interview_invite",
    priority: "high",
    company: "Northwind Labs",
    role: "Senior Frontend Engineer",
    tracker_row: "12",
    next_action: "Reply and confirm interview availability.",
    timestamp: "2026-08-10T09:00:00+00:00",
    from_domain: "northwind.example",
    actionable: true,
    ignore_reason: "",
    ...overrides,
  };
}

function stubApi(options: {
  events?: unknown[];
  hasRun?: boolean;
  atCap?: boolean;
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
      if (url.pathname === "/api/mail/events") {
        return reply(200, {
          events: options.events ?? [],
          has_run: options.hasRun ?? true,
          at_cap: options.atCap ?? false,
        });
      }
      if (url.pathname === "/api/mail/watch") {
        return reply(200, {
          id: "run9",
          kind: "gmail-watch",
          state: "running",
          created_at: "2026-01-01T00:00:00Z",
          started_at: null,
          ended_at: null,
          exit_code: null,
        });
      }
      if (url.pathname.startsWith("/api/runs/")) {
        return reply(200, {
          id: "run9",
          kind: "gmail-watch",
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
      <InboxPage createEventSource={() => new FakeEventSource() as unknown as EventSource} />
    </QueryClientProvider>,
  );
}

// --- three empty lists that mean different things ----------------------------

test("a watch that never ran does not read as an empty inbox", async () => {
  stubApi({ events: [], hasRun: false });
  renderPage();

  const message = await screen.findByText(/The watch has not run yet/);
  expect(message.textContent).toContain("Run it above");
});

test("a watch that ran and classified nothing says that instead", async () => {
  stubApi({ events: [], hasRun: true });
  renderPage();

  expect(await screen.findByText(/classified nothing/)).toBeTruthy();
  // Not the never-run message, which would send the operator to do something
  // they have already done.
  expect(screen.queryByText(/has not run yet/)).toBeNull();
});

test("an archive at its cap is presented as a window rather than a history", async () => {
  stubApi({ events: [event()], atCap: true });
  renderPage();

  const summary = await screen.findByText(/recent history, not all of it/);
  expect(summary).toBeTruthy();
});

test("a short archive is not described as a window", async () => {
  stubApi({ events: [event()], atCap: false });
  renderPage();

  await screen.findByText("Northwind Labs");
  expect(screen.queryByText(/recent history, not all of it/)).toBeNull();
});

// --- what the page shows, and what it cannot ---------------------------------

test("a row shows the classification and the action the classifier decided", async () => {
  stubApi({ events: [event()] });
  renderPage();

  expect(await screen.findByText("Interview invite")).toBeTruthy();
  expect(screen.getByText("Reply and confirm interview availability.")).toBeTruthy();
  expect(screen.getByText("northwind.example")).toBeTruthy();
});

test("the page says plainly that the message itself was never stored", async () => {
  stubApi({ events: [event()] });
  renderPage();

  const note = await screen.findByText(/never stored/);
  expect(note.textContent).toContain("subject");
  // And it points the operator at where a reply actually happens.
  expect(note.textContent).toContain("your own mail client");
});

test("an unmatched event still renders rather than being dropped", async () => {
  stubApi({ events: [event({ company: "", role: "", actionable: false, kind: "ignored" })] });
  renderPage();

  expect(await screen.findByText("unmatched")).toBeTruthy();
});

// --- running the watch -------------------------------------------------------

test("the dry-run choice reaches the request", async () => {
  const calls = stubApi({ events: [] });
  const user = userEvent.setup();
  renderPage();

  await user.click(screen.getByLabelText(/Dry run/));
  await user.click(screen.getByRole("button", { name: "Run the watch" }));

  await waitFor(() => {
    expect(calls.some((call) => call.url === "/api/mail/watch")).toBe(true);
  });
  const sent = calls.find((call) => call.url === "/api/mail/watch");
  expect(sent?.body).toEqual({ dry_run: true });
  expect(sent?.token).toBe("test-token");
});

test("a failed watch shows the reason the process printed", async () => {
  // A missing or expired Gmail token is the likeliest failure here and the
  // one with a specific fix, and the domain's own message names it.
  stubApi({ events: [], runState: "failed" });
  const user = userEvent.setup();
  renderPage();

  await user.click(screen.getByRole("button", { name: "Run the watch" }));
  await waitFor(() => {
    expect(FakeEventSource.last).not.toBeNull();
  });

  const source = FakeEventSource.last;
  await act(async () => {
    source?.emit({
      type: "log_line",
      line: "gmail watch failed: missing Gmail OAuth token file: /x/token.json. Run: harrier gmail-oauth",
    });
    source?.emit({ type: "state_change", state: "failed", exit_code: 1 });
    await Promise.resolve();
  });

  const alerts = await screen.findAllByRole("alert");
  expect(alerts.some((node) => node.textContent.includes("harrier gmail-oauth"))).toBe(true);
});

// --- the token boundary ------------------------------------------------------

test("reading the archive carries no token, unlike the artifact and contact reads", async () => {
  const calls = stubApi({ events: [event()] });
  renderPage();

  await waitFor(() => {
    expect(calls.some((call) => call.url === "/api/mail/events")).toBe(true);
  });
  const read = calls.find((call) => call.url === "/api/mail/events");
  // The archive was redacted on the way in, so there is no identifying
  // content here to protect (spec 049).
  expect(read?.method).toBe("GET");
  expect(read?.token).toBeNull();
});
