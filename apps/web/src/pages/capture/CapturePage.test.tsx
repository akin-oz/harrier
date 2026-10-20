import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { TEST_TOKEN, stubApi } from "../../shared/testing/stubApi";
import type { ApiReply } from "../../shared/testing/stubApi";
import { INDUSTRY_TRACK } from "../../shared/track/fixtures";
import { CapturePage } from "./CapturePage";
import { readCapture } from "./fragment";
import type { Captured } from "./fragment";

/**
 * The capture page (spec 119): a job handed over from a posting fills a form,
 * and nothing is written until the owner presses Add. Every value here is
 * synthetic.
 */

const FIELDS = {
  title: "Senior Frontend Engineer",
  company: "Example Co",
  location: "Remote, Europe",
  url: "https://jobs.example.com/postings/1",
  source: "linkedin",
  description: "Build the web app and its tests.",
};

const CAPTURED: Captured = { fields: FIELDS, linkLeftOut: false };

const ADD = "/api/capture/add";

function renderPage(captured: Captured = CAPTURED, onClose: () => void = vi.fn()) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <CapturePage captured={captured} trackLabel={INDUSTRY_TRACK.label} onClose={onClose} />
    </QueryClientProvider>,
  );
}

function answerAdd(reply: ApiReply) {
  return stubApi((call) => (call.path === ADD ? reply : undefined));
}

function addButton(): HTMLButtonElement {
  return screen.getByRole("button", { name: "Add to tracker" });
}

function input(label: string): HTMLInputElement {
  return screen.getByLabelText(label);
}

beforeEach(() => {
  window.history.replaceState(null, "", "/");
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

test("the form shows every field and the source", () => {
  answerAdd(undefined);
  renderPage();

  expect(screen.getByRole("heading", { name: "Add a job from a posting" })).toBeDefined();
  expect(input("Title").value).toBe(FIELDS.title);
  expect(input("Company").value).toBe(FIELDS.company);
  expect(input("Location").value).toBe(FIELDS.location);
  expect(input("Link").value).toBe(FIELDS.url);
  expect(screen.getByLabelText<HTMLTextAreaElement>("Description").value).toBe(FIELDS.description);
  expect(screen.getByText("Source: linkedin")).toBeDefined();
  expect(screen.getByText("Adds to Job search.")).toBeDefined();
  // A link that was kept is not named as left out.
  expect(screen.queryByText("The link was left out because it was not a web address.")).toBeNull();
});

test("the left-out link is named", () => {
  answerAdd(undefined);
  renderPage({ fields: { ...FIELDS, url: "" }, linkLeftOut: true });

  expect(screen.getByText("The link was left out because it was not a web address.")).toBeDefined();
  expect(input("Link").value).toBe("");
});

test("add waits for a title and a company", async () => {
  answerAdd(undefined);
  const user = userEvent.setup();

  renderPage({ fields: { ...FIELDS, title: "" }, linkLeftOut: false });
  expect(addButton().disabled).toBe(true);
  cleanup();

  renderPage({ fields: { ...FIELDS, company: "" }, linkLeftOut: false });
  expect(addButton().disabled).toBe(true);
  cleanup();

  renderPage({ fields: { ...FIELDS, title: "   ", company: "  " }, linkLeftOut: false });
  expect(addButton().disabled).toBe(true);
  await user.type(input("Title"), "Engineer");
  // A title alone is not enough.
  expect(addButton().disabled).toBe(true);
  await user.type(input("Company"), "Example Co");
  expect(addButton().disabled).toBe(false);
  // Emptying the title again takes Add away again.
  await user.clear(input("Title"));
  expect(addButton().disabled).toBe(true);
});

test("add is disabled while the request is in flight", async () => {
  const calls = answerAdd({ status: 200, body: { ok: true, message: "added" } });
  // Hold the add's answer until the test lets it go.
  const answer = globalThis.fetch;
  let release: () => void = () => undefined;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
      const response = await answer(request, init);
      const url = request instanceof Request ? request.url : String(request);
      if (new URL(url, "http://localhost").pathname === ADD) await held;
      return response;
    }),
  );
  const user = userEvent.setup();
  renderPage();

  await user.click(addButton());
  await waitFor(() => {
    expect(calls.filter((call) => call.path === ADD)).toHaveLength(1);
  });
  expect(addButton().disabled).toBe(true);
  // A second press, by click or by Enter in a field, sends nothing.
  await user.click(addButton());
  await user.type(input("Title"), "{Enter}");
  expect(calls.filter((call) => call.path === ADD)).toHaveLength(1);

  release();
  await waitFor(() => {
    expect(screen.getByRole("status").textContent).toBe("Added to Job search.");
  });
  expect(calls.filter((call) => call.path === ADD)).toHaveLength(1);
});

test("add posts the form once with its six fields", async () => {
  const calls = answerAdd({ status: 200, body: { ok: true, message: "added" } });
  const user = userEvent.setup();
  renderPage();

  await user.clear(input("Title"));
  await user.type(input("Title"), "Staff Frontend Engineer");
  await user.click(addButton());
  await waitFor(() => {
    expect(screen.getByRole("status").textContent).toBe("Added to Job search.");
  });

  const posts = calls.filter((call) => call.method === "POST");
  expect(posts).toHaveLength(1);
  expect(posts[0]?.path).toBe(ADD);
  expect(posts[0]?.body).toEqual({ ...FIELDS, title: "Staff Frontend Engineer" });
  expect(posts[0]?.token).toBe(TEST_TOKEN);
});

test("an add names the track it went to and offers no second add", async () => {
  answerAdd({ status: 200, body: { ok: true, message: "added" } });
  const onClose = vi.fn();
  const user = userEvent.setup();
  renderPage(CAPTURED, onClose);

  await user.click(addButton());
  await waitFor(() => {
    expect(screen.getByRole("status").textContent).toBe("Added to Job search.");
  });
  expect(screen.queryByRole("button", { name: "Add to tracker" })).toBeNull();
  expect(screen.queryByLabelText("Title")).toBeNull();
  expect(onClose).not.toHaveBeenCalled();

  await user.click(screen.getByRole("button", { name: "Open the tracker" }));
  expect(onClose).toHaveBeenCalledTimes(1);
});

test("a duplicate shows the server's message and keeps the form", async () => {
  const calls = answerAdd({ status: 409, body: { ok: false, message: "already in tracker" } });
  const onClose = vi.fn();
  const user = userEvent.setup();
  renderPage(CAPTURED, onClose);

  await user.click(addButton());
  await waitFor(() => {
    expect(screen.getByRole("status").textContent).toBe("already in tracker");
  });
  expect(input("Title").value).toBe(FIELDS.title);
  expect(addButton().disabled).toBe(false);
  expect(screen.queryByRole("button", { name: "Open the tracker" })).toBeNull();
  expect(onClose).not.toHaveBeenCalled();
  expect(calls.filter((call) => call.path === ADD)).toHaveLength(1);
});

test("a refusal without a message is read by the refusal reader", async () => {
  answerAdd({ status: 403, body: { detail: "missing or wrong local API token" } });
  const user = userEvent.setup();
  renderPage();

  await user.click(addButton());
  await waitFor(() => {
    expect(screen.getByRole("status").textContent).toBe("missing or wrong local API token");
  });
  expect(input("Title").value).toBe(FIELDS.title);
});

// The add's answer exactly as given, for a body the JSON stub cannot make.
function answerAddWith(response: () => Response) {
  const calls = answerAdd(undefined);
  const answer = globalThis.fetch;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
      const replied = await answer(request, init);
      const url = request instanceof Request ? request.url : String(request);
      return new URL(url, "http://localhost").pathname === ADD ? response() : replied;
    }),
  );
  return calls;
}

test("a refusal whose body is not JSON shows the fallback", async () => {
  // A proxy in front of the API answers with a page, not the API's JSON.
  answerAddWith(
    () =>
      new Response("<html>Bad Gateway</html>", {
        status: 502,
        headers: { "Content-Type": "text/html" },
      }),
  );
  const user = userEvent.setup();
  renderPage();

  await user.click(addButton());
  await waitFor(() => {
    expect(screen.getByRole("status").textContent).toBe("The job was not added.");
  });
  expect(input("Title").value).toBe(FIELDS.title);
});

test("a refusal whose detail is neither text nor field errors shows the fallback", async () => {
  answerAdd({ status: 403, body: { detail: { reason: "blocked" } } });
  const user = userEvent.setup();
  renderPage();

  await user.click(addButton());
  await waitFor(() => {
    expect(screen.getByRole("status").textContent).toBe("The job was not added.");
  });
  expect(input("Title").value).toBe(FIELDS.title);
});

test("an empty refusal is never read as an add", async () => {
  // The client hands back no error at all for an empty failed answer, so
  // only the status says it failed.
  answerAddWith(() => new Response(null, { status: 500, headers: { "Content-Length": "0" } }));
  const user = userEvent.setup();
  renderPage();

  await user.click(addButton());
  await waitFor(() => {
    expect(screen.getByRole("status").textContent).toBe("The job was not added.");
  });
  expect(addButton()).toBeDefined();
});

test("cancel writes nothing", async () => {
  const calls = answerAdd({ status: 200, body: { ok: true, message: "added" } });
  const onClose = vi.fn();
  const user = userEvent.setup();
  renderPage(CAPTURED, onClose);

  await user.click(screen.getByRole("button", { name: "Cancel" }));
  expect(onClose).toHaveBeenCalledTimes(1);
  expect(calls.filter((call) => call.method === "POST")).toEqual([]);
});

test("reading the page removes the fragment and keeps the track", () => {
  answerAdd(undefined);
  window.history.replaceState(null, "", "/?track=second-search#capture?title=x");
  const captured = readCapture(window.location.hash);
  expect(captured).not.toBeNull();
  renderPage(captured ?? CAPTURED);

  expect(input("Title").value).toBe("x");
  expect(window.location.hash).toBe("");
  expect(window.location.search).toBe("?track=second-search");
  expect(window.location.pathname).toBe("/");
});

test("no field reaches the page as markup", () => {
  answerAdd(undefined);
  const markup = "<img src=x onerror=alert(1)>";
  const { container } = renderPage({
    fields: { ...FIELDS, title: markup, description: markup, source: markup },
    linkLeftOut: false,
  });

  expect(input("Title").value).toBe(markup);
  expect(screen.getByLabelText<HTMLTextAreaElement>("Description").value).toBe(markup);
  expect(screen.getByText(`Source: ${markup}`)).toBeDefined();
  expect(container.querySelector("img")).toBeNull();
});

test("the status region is there before any message", () => {
  answerAdd(undefined);
  renderPage();

  const status = screen.getByRole("status");
  expect(status.textContent).toBe("");
  expect(addButton().disabled).toBe(false);
});
