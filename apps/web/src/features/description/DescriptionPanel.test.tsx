import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { DescriptionPanel } from "./DescriptionPanel";

/**
 * The description panel (spec 149): the stored description shown when there
 * is one, a paste box when there is none, and the stored text after a save.
 */

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

type Call = { method: string; url: string; body: unknown; token: string | null };

// Invented content throughout (ADR-008).
const POSTING = "Invented Labs is hiring a remote engineer to own an invented design system.";

function stubApi(options: {
  stored: { text: string; stored: boolean };
  put?: { status: number; body: unknown };
  fetch?: { status: number; body: unknown };
}): Call[] {
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
      if (request.method === "POST" && url.pathname.endsWith("/description/fetch")) {
        return options.fetch
          ? reply(options.fetch.status, options.fetch.body)
          : reply(200, { text: POSTING, stored: true });
      }
      if (request.method === "PUT") {
        if (options.put) return reply(options.put.status, options.put.body);
        const sent = JSON.parse(raw) as { text: string };
        return reply(200, { text: sent.text.trim(), stored: true });
      }
      return reply(200, options.stored);
    }),
  );
  return calls;
}

function renderPanel(): void {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <DescriptionPanel jobId={3} />
    </QueryClientProvider>,
  );
}

test("a stored description is shown, with no paste box", async () => {
  const calls = stubApi({ stored: { text: POSTING, stored: true } });
  renderPanel();

  expect((await screen.findByLabelText("Stored description")).textContent).toBe(POSTING);
  expect(screen.queryByLabelText("Paste the job description")).toBeNull();
  expect(screen.queryByText(/No description was captured/)).toBeNull();
  expect(calls.find((call) => call.url === "/api/apply/3/description")?.token).toBe("test-token");
});

test("no stored description shows the paste box, and saving shows the stored text", async () => {
  const calls = stubApi({ stored: { text: "", stored: false } });
  const user = userEvent.setup();
  renderPanel();

  expect(
    await screen.findByText(
      "No description was captured for this job. Paste it to use it for every operation.",
    ),
  ).toBeTruthy();
  const save = screen.getByRole("button", { name: "Save" });
  // Nothing to save until something is pasted.
  expect(save.hasAttribute("disabled")).toBe(true);

  await user.click(screen.getByLabelText("Paste the job description"));
  await user.paste(`  ${POSTING}\n`);
  await user.click(save);

  // What the server stored, trimmed, not what was pasted.
  expect((await screen.findByLabelText("Stored description")).textContent).toBe(POSTING);
  expect(screen.queryByLabelText("Paste the job description")).toBeNull();
  expect(screen.getByRole("status").textContent).toBe(
    "Saved. Every operation reads this description.",
  );
  // Focus lands on a control that still exists, not on the page.
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Replace" }));
  const put = calls.find((call) => call.method === "PUT");
  expect(put?.url).toBe("/api/apply/3/description");
  expect(put?.body).toEqual({ text: `  ${POSTING}\n` });
});

test("replace opens the box with the stored text, and cancel closes it", async () => {
  stubApi({ stored: { text: POSTING, stored: true } });
  const user = userEvent.setup();
  renderPanel();

  await user.click(await screen.findByRole("button", { name: "Replace" }));
  const box = screen.getByLabelText("Paste the job description");
  expect(box).toHaveProperty("value", POSTING);
  expect(document.activeElement).toBe(box);
  await user.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.queryByLabelText("Paste the job description")).toBeNull();
  expect(screen.getByLabelText("Stored description").textContent).toBe(POSTING);
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Replace" }));
});

test("show all expands the stored text", async () => {
  stubApi({ stored: { text: POSTING, stored: true } });
  const user = userEvent.setup();
  renderPanel();

  const toggle = await screen.findByRole("button", { name: "Show all" });
  const text = screen.getByLabelText("Stored description");
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  expect(text.classList.contains("description-panel__text--collapsed")).toBe(true);
  await user.click(toggle);
  expect(screen.getByRole("button", { name: "Show less" }).getAttribute("aria-expanded")).toBe(
    "true",
  );
  expect(
    screen
      .getByLabelText("Stored description")
      .classList.contains("description-panel__text--collapsed"),
  ).toBe(false);
});

test("a refusal is shown in the API's words and the pasted text is kept", async () => {
  stubApi({
    stored: { text: "", stored: false },
    put: { status: 400, body: { detail: "the description is longer than 65536 bytes in UTF-8" } },
  });
  const user = userEvent.setup();
  renderPanel();

  await user.click(await screen.findByLabelText("Paste the job description"));
  await user.paste(POSTING);
  await user.click(screen.getByRole("button", { name: "Save" }));

  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toBe("the description is longer than 65536 bytes in UTF-8");
  await waitFor(() => {
    expect(screen.getByLabelText("Paste the job description")).toHaveProperty("value", POSTING);
  });
});

test("fetch from the posting stores and shows the description (spec 151)", async () => {
  const calls = stubApi({ stored: { text: "", stored: false } });
  const user = userEvent.setup();
  renderPanel();

  await user.click(await screen.findByRole("button", { name: "Fetch from the posting" }));

  expect((await screen.findByLabelText("Stored description")).textContent).toBe(POSTING);
  expect(screen.getByRole("status").textContent).toBe(
    "Fetched from the posting. Every operation reads this description.",
  );
  expect(screen.queryByRole("button", { name: "Fetch from the posting" })).toBeNull();
  const post = calls.find((call) => call.method === "POST");
  expect(post?.url).toBe("/api/apply/3/description/fetch");
  expect(post?.token).toBe("test-token");
});

test("a fetch refusal is shown in the API's words and the paste box stays", async () => {
  stubApi({
    stored: { text: "", stored: false },
    fetch: { status: 502, body: { detail: "the posting page held no description" } },
  });
  const user = userEvent.setup();
  renderPanel();

  await user.click(await screen.findByLabelText("Paste the job description"));
  await user.paste("half a pasted posting");
  await user.click(screen.getByRole("button", { name: "Fetch from the posting" }));

  expect((await screen.findByRole("alert")).textContent).toBe(
    "the posting page held no description",
  );
  expect(screen.getByLabelText("Paste the job description")).toHaveProperty(
    "value",
    "half a pasted posting",
  );
});

test("an earlier fetch refusal does not reappear after a save and Replace", async () => {
  stubApi({
    stored: { text: "", stored: false },
    fetch: { status: 502, body: { detail: "the posting page held no description" } },
  });
  const user = userEvent.setup();
  renderPanel();

  await user.click(await screen.findByRole("button", { name: "Fetch from the posting" }));
  expect(await screen.findByRole("alert")).toBeTruthy();
  await user.click(screen.getByLabelText("Paste the job description"));
  await user.paste(POSTING);
  await user.click(screen.getByRole("button", { name: "Save" }));
  await user.click(await screen.findByRole("button", { name: "Replace" }));

  expect(screen.getByLabelText("Paste the job description")).toHaveProperty("value", POSTING);
  expect(screen.queryByRole("alert")).toBeNull();
});
