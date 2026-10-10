import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { BriefPanel } from "./BriefPanel";

/**
 * The Brief panel (spec 095): the brief as fields, saved whole in one
 * request, refused in the store's words with the input kept.
 */

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

type Call = { method: string; url: string; body: unknown; token: string | null };

// Invented content throughout (ADR-008).
const STORED = {
  never_name: ["Invented Client Ltd"],
  guidance_url: "https://careers.example.com/guidance",
  employer_guidance: "Say what you built.",
  letter: { max_words: 250, max_sentences: null, paragraphs: 3 },
  answers: { max_words: null, max_sentences: 4 },
  evidence: ["an invented line of evidence"],
  views: { "Why us?": "An invented view." },
  compensation_number: "100",
  confirmed_skills: ["Invented Skill"],
};

function stubApi(options: { stored?: unknown; put?: { status: number; body: unknown } }): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(String(input), init);
      const url = new URL(request.url);
      const raw = request.method === "GET" ? "" : await request.clone().text();
      calls.push({
        method: request.method,
        url: url.pathname,
        body: raw === "" ? null : (JSON.parse(raw) as unknown),
        token: request.headers.get("X-Harrier-Token"),
      });
      const reply = (status: number, body: unknown): Response =>
        new Response(JSON.stringify(body), {
          status,
          headers: { "Content-Type": "application/json" },
        });
      if (url.pathname === "/api/session") return reply(200, { token: "test-token" });
      if (request.method === "PUT") {
        return options.put ? reply(options.put.status, options.put.body) : reply(200, STORED);
      }
      if (options.stored === undefined) return reply(404, { detail: "no brief for job 3" });
      return reply(200, options.stored);
    }),
  );
  return calls;
}

function renderPanel(): void {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <BriefPanel jobId={3} />
    </QueryClientProvider>,
  );
}

test("a stored brief is shown as fields, not JSON", async () => {
  const calls = stubApi({ stored: STORED });
  renderPanel();

  expect(await screen.findByDisplayValue("Invented Client Ltd", { exact: true })).toBeTruthy();
  expect(screen.getByLabelText("Letter words")).toHaveProperty("value", "250");
  expect(screen.getByLabelText("Letter paragraphs")).toHaveProperty("value", "3");
  expect(screen.getByLabelText("Answer sentences")).toHaveProperty("value", "4");
  expect(screen.getByLabelText("Guidance URL")).toHaveProperty(
    "value",
    "https://careers.example.com/guidance",
  );
  expect(screen.getByLabelText("Question 1")).toHaveProperty("value", "Why us?");
  expect(screen.getByLabelText("Compensation number")).toHaveProperty("value", "100");
  // The read carries the token: a brief is the operator's own notes.
  expect(calls.find((call) => call.url === "/api/apply/3/brief")?.token).toBe("test-token");
});

test("saving sends the whole brief in one request, keeping what it does not edit", async () => {
  const calls = stubApi({ stored: STORED });
  const user = userEvent.setup();
  renderPanel();

  const words = await screen.findByLabelText("Letter words");
  await user.clear(words);
  await user.type(words, "300");
  await user.click(screen.getByRole("button", { name: "Save the brief" }));
  await waitFor(() => {
    expect(calls.filter((call) => call.method === "PUT")).toHaveLength(1);
  });
  const sent = calls.find((call) => call.method === "PUT");
  expect(sent?.url).toBe("/api/apply/3/brief");
  expect(sent?.body).toEqual({
    never_name: ["Invented Client Ltd"],
    guidance_url: "https://careers.example.com/guidance",
    employer_guidance: "Say what you built.",
    letter: { max_words: 300, paragraphs: 3 },
    answers: { max_sentences: 4 },
    evidence: ["an invented line of evidence"],
    views: { "Why us?": "An invented view." },
    compensation_number: "100",
    confirmed_skills: ["Invented Skill"],
  });
  expect(await screen.findByText("Brief saved.")).toBeTruthy();
});

test("a refusal is shown in the store's words and the input is kept", async () => {
  stubApi({
    put: { status: 400, body: { detail: "letter.max_words must be a positive integer" } },
  });
  const user = userEvent.setup();
  renderPanel();

  expect(await screen.findByText(/Nothing is stored for this job yet/)).toBeTruthy();
  const words = screen.getByLabelText("Letter words");
  await user.type(words, "0");
  await user.type(screen.getByLabelText("Evidence, one entry per line"), "an invented entry");
  await user.click(screen.getByRole("button", { name: "Save the brief" }));

  expect((await screen.findByRole("alert")).textContent).toBe(
    "letter.max_words must be a positive integer",
  );
  expect(screen.getByLabelText("Letter words")).toHaveProperty("value", "0");
  expect(screen.getByLabelText("Evidence, one entry per line")).toHaveProperty(
    "value",
    "an invented entry",
  );
});

test("a question can be added and removed", async () => {
  stubApi({});
  const user = userEvent.setup();
  renderPanel();

  await user.click(await screen.findByRole("button", { name: "Add a question" }));
  await user.type(screen.getByLabelText("Question 1"), "Why this team?");
  expect(screen.getByLabelText("Question 1")).toHaveProperty("value", "Why this team?");
  await user.click(screen.getByRole("button", { name: "Remove question 1" }));
  expect(screen.queryByLabelText("Question 1")).toBeNull();
});
