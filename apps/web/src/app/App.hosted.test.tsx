import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

// The hosted build (spec 114). A hosted bundle is fixed when it is built, so
// the hosted app's tests are their own file: the mode is read before any
// module is.
vi.hoisted(() => {
  vi.stubEnv("VITE_HARRIER_AUTH", "supabase");
  vi.stubEnv("VITE_SUPABASE_URL", "https://abc.supabase.co");
  vi.stubEnv("VITE_SUPABASE_PUBLISHABLE_KEY", "sb_publishable_synthetic");
  vi.stubEnv("VITE_HARRIER_API_URL", "https://api.example.com");
});

vi.mock("@supabase/supabase-js", async () => ({
  createClient: (await import("../shared/testing/fakeSupabase")).fake.createClient,
}));

import { SESSION_A, SESSION_B, fake } from "../shared/testing/fakeSupabase";
import { installRunStreams } from "../shared/testing/runStreams";
import type { RunStreams } from "../shared/testing/runStreams";
import { ACADEMIC_TRACK, INDUSTRY_TRACK } from "../shared/track/fixtures";
import { App, queryClient } from "./App";

function row(company: string) {
  return {
    id: 1,
    company,
    title: "Senior Frontend Engineer",
    url: "https://boards.example.com/x/1",
    location: "Remote, Europe",
    source: "greenhouse",
    status: "prospect",
    score: "80",
    fit_score: "",
    next_action: "",
    added_at: "2026-01-01",
    track: "job",
    deadline: "",
    deadline_passed: false,
    eligibility: {
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
    },
  };
}

const RUN = {
  id: "run1",
  kind: "demo",
  state: "queued",
  created_at: "t",
  started_at: null,
  ended_at: null,
  exit_code: null,
};

interface Call {
  method: string;
  url: string;
  authorization: string | null;
  localToken: string | null;
}

let calls: Call[] = [];
// What the stub answers for one path instead of its usual answer.
let override: ((path: string) => Response | Promise<Response> | undefined) | null = null;
let jobs: () => Response | Promise<Response>;
let streams: RunStreams;

function reply(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubHosted(): void {
  calls = [];
  override = null;
  jobs = () => reply([row("Example Co")]);
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(String(input));
      calls.push({
        method: request.method,
        url: request.url,
        authorization: request.headers.get("Authorization"),
        localToken: request.headers.get("X-Harrier-Token"),
      });
      const path = new URL(request.url).pathname;
      const overridden = override?.(path);
      if (overridden !== undefined) return Promise.resolve(overridden);
      if (path === "/tracks") return Promise.resolve(reply([INDUSTRY_TRACK, ACADEMIC_TRACK]));
      if (path === "/jobs") return Promise.resolve(jobs());
      if (path === "/health") return Promise.resolve(reply({ status: "ok" }));
      if (path === "/runs") return Promise.resolve(reply(RUN));
      if (path === "/runs/run1") return Promise.resolve(reply(RUN));
      if (path === "/ops/schedule") {
        return Promise.resolve(
          // The hosted answer's shape (spec 120): no host command.
          reply({
            jobs: [],
            error: null,
            installed_state: "",
            host_command: null,
            timezone: null,
            offered: [],
          }),
        );
      }
      return Promise.resolve(reply([]));
    }),
  );
  streams = installRunStreams();
}

beforeEach(() => {
  fake.reset();
  fake.signIn(SESSION_A);
  window.history.replaceState(null, "", "/");
  stubHosted();
});

afterEach(() => {
  cleanup();
  queryClient.clear();
  vi.unstubAllGlobals();
});

async function startRun(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  await user.click(await screen.findByRole("button", { name: "Start demo run" }));
  await waitFor(() => {
    expect(streams.connections).toHaveLength(1);
  });
}

test("hosted without a session shows only sign-in", async () => {
  fake.signOutElsewhere();
  render(<App />);

  expect(await screen.findByLabelText("Email address")).toBeDefined();
  // No header, no sections, and nothing asked of the API.
  expect(screen.queryByRole("banner")).toBeNull();
  expect(screen.queryByRole("navigation", { name: "Sections" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Sign out" })).toBeNull();
  await new Promise((resolve) => setTimeout(resolve, 30));
  expect(calls).toEqual([]);
});

test("a verified code opens the app", async () => {
  fake.signOutElsewhere();
  const user = userEvent.setup();
  render(<App />);

  await user.type(await screen.findByLabelText("Email address"), "person@example.invalid");
  await user.click(screen.getByRole("button", { name: "Send a sign-in code" }));
  await user.type(await screen.findByLabelText("Sign-in code"), "123456");
  await user.click(screen.getByRole("button", { name: "Sign in" }));

  expect(await screen.findByText("Example Co")).toBeDefined();
  expect(screen.getByRole("button", { name: "Sign out" })).toBeDefined();
  expect(calls.every((call) => call.authorization === "Bearer verified-token")).toBe(true);
});

test("the hosted app sends the bearer token on every request it makes", async () => {
  const user = userEvent.setup();
  render(<App />);
  await screen.findByText("Example Co");
  await startRun(user);

  expect(calls.some((call) => call.method === "POST")).toBe(true);
  expect(calls.length).toBeGreaterThan(4);
  for (const call of calls) {
    expect(call.authorization, call.url).toBe("Bearer token-a");
    expect(call.localToken, call.url).toBeNull();
    expect(new URL(call.url).origin, call.url).toBe("https://api.example.com");
    expect(new URL(call.url).pathname, call.url).not.toBe("/session");
    expect(call.url).not.toContain("token-a");
  }
  // The run stream is one of them.
  expect(streams.last.authorization).toBe("Bearer token-a");
  expect(streams.last.path).toBe("/runs/run1/events");
  // The local app's header is not hosted's: there is a way to sign out.
  expect(screen.getByRole("button", { name: "Sign out" })).toBeDefined();
});

test("a refused token ends the session", async () => {
  const cases = [
    {
      name: "invalid",
      status: 401,
      reason: "invalid",
      sentence: "Your session ended. Sign in again.",
    },
    {
      name: "missing",
      status: 401,
      reason: "missing",
      sentence: "Your session ended. Sign in again.",
    },
    {
      name: "anonymous",
      status: 403,
      reason: "anonymous",
      sentence: "This account cannot use harrier.",
    },
  ];
  for (const { name, status, reason, sentence } of cases) {
    fake.reset();
    fake.signIn(SESSION_A);
    stubHosted();
    override = (path) =>
      path === "/tracks" ? reply({ reason, detail: "refused" }, status) : undefined;
    render(<App />);

    expect((await screen.findByRole("alert")).textContent, name).toBe(sentence);
    expect(screen.getByLabelText("Email address"), name).toBeDefined();
    // No retry, and the session was ended once.
    expect(
      calls.filter((call) => new URL(call.url).pathname === "/tracks"),
      name,
    ).toHaveLength(1);
    expect(fake.log, name).toEqual(["signOut:local"]);
    cleanup();
  }
});

test("unavailable keys never sign anyone out", async () => {
  override = (path) =>
    path === "/tracks"
      ? reply(
          { reason: "keys_unavailable", detail: "The signing keys cannot be fetched now." },
          503,
        )
      : undefined;
  render(<App />);

  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toContain("The signing keys cannot be fetched now.");
  expect(screen.getByRole("button", { name: "Sign out" })).toBeDefined();
  expect(fake.log).toEqual([]);
});

test("sign-out forgets the person's data", async () => {
  const user = userEvent.setup();
  render(<App />);
  await screen.findByText("Example Co");
  await startRun(user);
  expect(queryClient.getQueryCache().getAll().length).toBeGreaterThan(0);

  // What happens, in the order it happens.
  streams.last.signal.addEventListener("abort", () => {
    fake.log.push("stream aborted");
  });
  const unsubscribe = queryClient.getQueryCache().subscribe(() => {
    if (queryClient.getQueryCache().getAll().length === 0 && !fake.log.includes("cache emptied")) {
      fake.log.push("cache emptied");
    }
  });
  await user.click(screen.getByRole("button", { name: "Sign out" }));

  expect(await screen.findByLabelText("Email address")).toBeDefined();
  unsubscribe();
  expect(fake.log).toEqual(["signOut:local", "stream aborted", "cache emptied"]);
  expect(queryClient.getQueryCache().getAll()).toHaveLength(0);
  // Signing out is the person's own act, so the page says nothing about it.
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.queryByText("Example Co")).toBeNull();
});

// Production runs the app under StrictMode, which renders twice.
async function swapUsers(strict: boolean): Promise<void> {
  render(
    strict ? (
      <StrictMode>
        <App />
      </StrictMode>
    ) : (
      <App />
    ),
  );
  await screen.findByText("Example Co");

  // Another tab signs in as someone else; their rows are slow to arrive.
  let release: () => void = () => undefined;
  jobs = () =>
    new Promise<Response>((resolve) => {
      release = () => {
        resolve(reply([row("Other Co")]));
      };
    });
  fake.signIn(SESSION_B);

  await waitFor(() => {
    expect(calls.some((call) => call.authorization === "Bearer token-b")).toBe(true);
  });
  // While the new person's rows load, the old person's are not on screen.
  expect(screen.queryByText("Example Co")).toBeNull();
  release();
  expect(await screen.findByText("Other Co")).toBeDefined();
  expect(screen.queryByText("Example Co")).toBeNull();
}

test("a different user never sees the previous user's cache", async () => {
  await swapUsers(false);
});

test("a different user never sees the previous user's cache under StrictMode", async () => {
  await swapUsers(true);
});

test("a refresh token the server refuses ends the session with its sentence", async () => {
  // supabase-js removes the session itself and fires the sign-out before the
  // refresh returns; the page must still say why.
  fake.refreshTo = null;
  override = (path) =>
    path === "/tracks"
      ? reply({ reason: "expired", detail: "The bearer token has expired." }, 401)
      : undefined;
  render(<App />);

  expect((await screen.findByRole("alert")).textContent).toBe("Your session ended. Sign in again.");
  expect(screen.getByLabelText("Email address")).toBeDefined();
  expect(fake.log).toEqual(["refreshSession"]);
});

test("a request that gets no answer shows the sentence for it", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))),
  );
  render(<App />);

  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toContain(
    "The harrier API did not answer. It may be down, or it may have failed on this request.",
  );
  // Still signed in: a failed request is not a refused token.
  expect(screen.getByRole("button", { name: "Sign out" })).toBeDefined();
  expect(fake.log).toEqual([]);
});

test("a capture survives sign-in", async () => {
  // A job handed over from a posting while signed out (spec 119). The values
  // are synthetic.
  fake.signOutElsewhere();
  const fields = {
    title: "Platform Engineer",
    company: "Sample Labs",
    location: "Remote, Europe",
    url: "https://jobs.example.com/postings/2",
    source: "linkedin",
    description: "Run the platform.",
  };
  window.history.replaceState(null, "", `/#capture?${new URLSearchParams(fields).toString()}`);
  override = (path) =>
    path === "/capture/add" ? reply({ ok: true, message: "added" }) : undefined;
  const user = userEvent.setup();
  render(<App />);

  await user.type(await screen.findByLabelText("Email address"), "person@example.invalid");
  await user.click(screen.getByRole("button", { name: "Send a sign-in code" }));
  await user.type(await screen.findByLabelText("Sign-in code"), "123456");
  await user.click(screen.getByRole("button", { name: "Sign in" }));

  expect(await screen.findByRole("heading", { name: "Add a job from a posting" })).toBeDefined();
  expect(screen.getByLabelText<HTMLInputElement>("Title").value).toBe(fields.title);
  expect(screen.getByLabelText<HTMLInputElement>("Company").value).toBe(fields.company);
  expect(screen.getByLabelText<HTMLInputElement>("Location").value).toBe(fields.location);
  expect(screen.getByLabelText<HTMLInputElement>("Link").value).toBe(fields.url);
  expect(screen.getByLabelText<HTMLTextAreaElement>("Description").value).toBe(fields.description);
  expect(screen.getByText("Source: linkedin")).toBeDefined();

  await user.click(screen.getByRole("button", { name: "Add to tracker" }));
  expect((await screen.findByText("Added to Job search.")).getAttribute("role")).toBe("status");
  const adds = calls.filter((call) => call.url === "https://api.example.com/capture/add");
  expect(adds).toHaveLength(1);
  expect(adds[0]?.method).toBe("POST");
  expect(adds[0]?.authorization).toBe("Bearer verified-token");
  expect(adds[0]?.localToken).toBeNull();
});
