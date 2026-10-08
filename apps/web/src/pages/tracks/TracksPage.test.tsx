import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import type { Track } from "../../shared/track";
import { ACADEMIC_TRACK, INDUSTRY_TRACK } from "../../shared/track/fixtures";
import { TracksPage } from "./TracksPage";

/**
 * `harrier tracks list|add|archive` in the browser (spec 094). The page shows
 * what the routes answer, offers only what they accept, and asks before the
 * one write that cannot be undone.
 */

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

// The domain's refusal, as `GET /tracks/kinds` carries it. Synthetic here;
// services/api/tests/test_api_tracks.py pins that the route answers with the
// same words `POST /tracks` refuses with.
const INDUSTRY_REASON = "a second industry track is not supported yet";

type Call = { url: string; method: string; body: unknown };

function stubApi(options: { tracks?: Track[]; archive?: { code: number; body: unknown } } = {}) {
  const calls: Call[] = [];
  let tracks = options.tracks ?? [INDUSTRY_TRACK, ACADEMIC_TRACK];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(String(input), init);
      const url = new URL(request.url);
      const raw = request.method === "GET" ? "" : await request.clone().text();
      calls.push({
        url: url.pathname + url.search,
        method: request.method,
        body: raw === "" ? null : JSON.parse(raw),
      });
      const reply = (body: unknown, status = 200): Response =>
        new Response(JSON.stringify(body), {
          status,
          headers: { "Content-Type": "application/json" },
        });
      if (url.pathname === "/api/session") return reply({ token: "test-token" });
      if (url.pathname === "/api/tracks/kinds") {
        return reply([
          { kind: "industry", available: false, reason: INDUSTRY_REASON },
          { kind: "academic", available: true, reason: "" },
        ]);
      }
      if (url.pathname === "/api/tracks" && request.method === "GET") return reply(tracks);
      if (url.pathname === "/api/tracks" && request.method === "POST") {
        const body = JSON.parse(raw) as { slug: string; label: string; kind: string };
        const added: Track = {
          ...ACADEMIC_TRACK,
          id: tracks.length + 1,
          slug: body.slug,
          label: body.label,
        };
        tracks = [...tracks, added];
        return reply(added, 201);
      }
      if (url.pathname.endsWith("/archive")) {
        if (options.archive !== undefined) return reply(options.archive.body, options.archive.code);
        tracks = tracks.map((track) =>
          track.slug === ACADEMIC_TRACK.slug ? { ...track, archived: true } : track,
        );
        return reply({ ...ACADEMIC_TRACK, archived: true });
      }
      return reply({ detail: `unstubbed ${url.pathname}` }, 404);
    }),
  );
  return calls;
}

function renderPage(onOpen: (track: Track) => void = () => undefined): void {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <TracksPage onOpen={onOpen} />
    </QueryClientProvider>,
  );
}

async function itemFor(label: string): Promise<HTMLElement> {
  const list = await screen.findByRole("list", { name: "Tracks" });
  const item = within(list).getByText(label).closest("li");
  if (item === null) throw new Error(`no item for ${label}`);
  return item;
}

test("every track is listed with its slug, kind and state, archived ones included", async () => {
  stubApi({ tracks: [INDUSTRY_TRACK, { ...ACADEMIC_TRACK, archived: true }] });
  renderPage();

  const first = await itemFor("Job search");
  expect(within(first).getByText("job")).toBeDefined();
  expect(within(first).getByText("industry")).toBeDefined();
  expect(first.textContent).toContain("Default, selected");
  // The default track is never offered for archiving.
  expect(within(first).queryByRole("button", { name: /^Archive/ })).toBeNull();

  const second = await itemFor("Second search");
  expect(within(second).getByText("second-search")).toBeDefined();
  expect(within(second).getByText("academic")).toBeDefined();
  expect(second.textContent).toContain("Archived");
  expect(within(second).queryByRole("button", { name: /^Archive/ })).toBeNull();
});

test("the industry kind is shown disabled with the domain's reason", async () => {
  stubApi();
  renderPage();

  const industry = await screen.findByRole("radio", { name: /industry/ });
  expect((industry as HTMLInputElement).disabled).toBe(true);
  expect(industry.getAttribute("aria-describedby")).not.toBeNull();
  expect(screen.getByText(`Not available: ${INDUSTRY_REASON}`)).toBeDefined();
  const academic = screen.getByRole("radio", { name: /academic/ });
  expect((academic as HTMLInputElement).disabled).toBe(false);
  expect((academic as HTMLInputElement).checked).toBe(true);
});

test("adding a track posts the slug, label and kind, and offers to open it", async () => {
  const calls = stubApi();
  const opened: Track[] = [];
  const user = userEvent.setup();
  renderPage((track) => opened.push(track));

  await screen.findByRole("radio", { name: /academic/ });
  await user.type(screen.getByLabelText("Label"), "Third search");
  // The slug is drafted from the label until it is edited.
  expect(screen.getByLabelText<HTMLInputElement>("Slug").value).toBe("third-search");
  await user.click(screen.getByRole("button", { name: "Add track" }));

  await waitFor(() => {
    expect(calls.some((call) => call.method === "POST" && call.url === "/api/tracks")).toBe(true);
  });
  const sent = calls.find((call) => call.method === "POST" && call.url === "/api/tracks");
  expect(sent?.body).toEqual({ slug: "third-search", label: "Third search", kind: "academic" });
  const outcome = (await screen.findByText("Added Third search.")).parentElement;
  if (outcome === null) throw new Error("no outcome");
  await itemFor("Third search");

  await user.click(within(outcome).getByRole("button", { name: "Open Third search" }));
  expect(opened.map((track) => track.slug)).toEqual(["third-search"]);
});

test("archiving asks first, by name, and Escape keeps the track", async () => {
  const calls = stubApi();
  const user = userEvent.setup();
  renderPage();

  const item = await itemFor("Second search");
  const opener = within(item).getByRole("button", { name: "Archive Second search" });
  await user.click(opener);
  const confirm = within(item).getByRole("group", { name: "Archive Second search" });
  expect(confirm.textContent).toContain("This cannot be undone.");
  // Focus goes to the safe answer.
  expect(document.activeElement).toBe(within(confirm).getByRole("button", { name: "Cancel" }));

  await user.keyboard("{Escape}");
  expect(within(item).queryByRole("group", { name: "Archive Second search" })).toBeNull();
  await waitFor(() => {
    expect(document.activeElement?.getAttribute("aria-label")).toBe("Archive Second search");
  });
  expect(calls.some((call) => call.url.endsWith("/archive"))).toBe(false);

  await user.keyboard("{Enter}");
  const again = within(item).getByRole("group", { name: "Archive Second search" });
  await user.click(within(again).getByRole("button", { name: "Archive Second search" }));
  await waitFor(() => {
    expect(calls.some((call) => call.url === "/api/tracks/second-search/archive")).toBe(true);
  });
  const notice = await screen.findByText("Archived Second search. Its rows stay readable.");
  // The Archive button left with the track's live state; focus goes to what
  // happened instead of to nothing.
  await waitFor(() => {
    expect(document.activeElement).toBe(notice);
  });
});

test("a refused archive is shown in the domain's words", async () => {
  const detail = "track 'second-search' is already archived";
  stubApi({ archive: { code: 409, body: { detail } } });
  const user = userEvent.setup();
  renderPage();

  const item = await itemFor("Second search");
  await user.click(within(item).getByRole("button", { name: "Archive Second search" }));
  const confirm = within(item).getByRole("group", { name: "Archive Second search" });
  await user.click(within(confirm).getByRole("button", { name: "Archive Second search" }));
  expect(await within(item).findByText(detail)).toBeDefined();
});
