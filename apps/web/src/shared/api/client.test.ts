import { afterEach, beforeEach, expect, test, vi } from "vitest";

// The hosted build, fixed before any module is read: the mode is chosen when a
// bundle is built (spec 114), so a hosted test file is a hosted bundle.
vi.hoisted(() => {
  vi.stubEnv("VITE_HARRIER_AUTH", "supabase");
  vi.stubEnv("VITE_SUPABASE_URL", "https://abc.supabase.co");
  vi.stubEnv("VITE_SUPABASE_PUBLISHABLE_KEY", "sb_publishable_synthetic");
  vi.stubEnv("VITE_HARRIER_API_URL", "https://api.example.com");
});

vi.mock("@supabase/supabase-js", async () => ({
  createClient: (await import("../testing/fakeSupabase")).fake.createClient,
}));

import { SESSION_A, SESSION_B, fake } from "../testing/fakeSupabase";
import { installRunStreams } from "../testing/runStreams";
import { UNREACHABLE, api } from "./client";
import { readRunEvents } from "./stream";

interface Seen {
  method: string;
  url: string;
  authorization: string | null;
  localToken: string | null;
  // The body as this attempt read it, which a retry must be able to do again.
  body: string;
}

let seen: Seen[] = [];

/** Answers every API request from `reply`, recording what was sent. */
function stubFetch(reply: (seen: Seen[]) => Response): void {
  seen = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(String(input));
      seen.push({
        method: request.method,
        url: request.url,
        authorization: request.headers.get("Authorization"),
        localToken: request.headers.get("X-Harrier-Token"),
        // Read the way a real fetch reads it: once, and for good.
        body: await request.text(),
      });
      return reply(seen);
    }),
  );
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const REFUSAL = {
  expired: { reason: "expired", detail: "The bearer token has expired." },
  invalid: { reason: "invalid", detail: "The bearer token could not be verified." },
  missing: { reason: "missing", detail: "This request carries no bearer token." },
  anonymous: { reason: "anonymous", detail: "An anonymous session cannot use the hosted API." },
  keys: { reason: "keys_unavailable", detail: "The signing keys cannot be fetched now." },
} as const;

beforeEach(() => {
  fake.reset();
  fake.signIn(SESSION_A);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

test("hosted calls carry the bearer token and nothing else", async () => {
  stubFetch(() => json(200, []));
  const streams = installRunStreams();

  // A tracker read, a tracker write, an operations write and a run stream.
  await api.GET("/tracks");
  await api.POST("/tracker/{selector}/status", {
    params: { path: { selector: "1" }, query: {} },
    body: { verb: "shortlist", reason: null },
  });
  await api.POST("/ops/feeds");
  const events = (async () => {
    for await (const message of readRunEvents("r1", {
      lastEventId: 0,
      signal: new AbortController().signal,
    })) {
      void message;
    }
  })();
  await vi.waitFor(() => {
    expect(streams.connections).toHaveLength(1);
  });
  streams.last.end();
  await events;

  const all = [...seen.map((call) => call.authorization), streams.last.authorization];
  expect(all).toEqual(Array<string>(4).fill("Bearer token-a"));
  expect(seen.every((call) => call.localToken === null)).toBe(true);
  expect(streams.last.localToken).toBeNull();
  // The API is where VITE_HARRIER_API_URL says, not the page's origin, and
  // the local token's route is never asked for.
  expect(seen.every((call) => new URL(call.url).origin === "https://api.example.com")).toBe(true);
  expect(seen.some((call) => new URL(call.url).pathname === "/session")).toBe(false);
  expect(seen.some((call) => call.url.includes("token-a"))).toBe(false);
  expect(streams.last.path).toBe("/runs/r1/events");
});

test("an expired token is refreshed once and retried", async () => {
  stubFetch((calls) => (calls.length === 1 ? json(401, REFUSAL.expired) : json(200, [])));

  const { data, error } = await api.GET("/tracks");

  expect(error).toBeUndefined();
  expect(data).toEqual([]);
  // The retried request carries the new token; the session stays.
  expect(seen.map((call) => call.authorization)).toEqual([
    "Bearer token-a",
    "Bearer refreshed-token",
  ]);
  expect(fake.log).toEqual(["refreshSession"]);

  // A second refusal ends the session, with no third request.
  fake.reset();
  fake.signIn(SESSION_A);
  stubFetch(() => json(401, REFUSAL.expired));
  const refused = await api.GET("/tracks");
  expect(refused.error).toBeDefined();
  expect(seen).toHaveLength(2);
  expect(fake.log).toEqual(["refreshSession", "signOut:local"]);

  // A retry the API refuses as anonymous ends the session as well.
  fake.reset();
  fake.signIn(SESSION_A);
  stubFetch((calls) =>
    calls.length === 1 ? json(401, REFUSAL.expired) : json(403, REFUSAL.anonymous),
  );
  await api.GET("/tracks");
  expect(fake.log).toEqual(["refreshSession", "signOut:local"]);
});

test("concurrent expired refusals share one refresh", async () => {
  // Refresh tokens are single use: two refreshes at once would revoke the
  // session. Both requests are retried on the one new token.
  stubFetch((calls) => (calls.length <= 2 ? json(401, REFUSAL.expired) : json(200, [])));

  const [first, second] = await Promise.all([api.GET("/tracks"), api.GET("/tracks/kinds")]);

  expect(first.error).toBeUndefined();
  expect(second.error).toBeUndefined();
  expect(fake.log).toEqual(["refreshSession"]);
  expect(seen.slice(2).map((call) => call.authorization)).toEqual([
    "Bearer refreshed-token",
    "Bearer refreshed-token",
  ]);
});

test("a refusal of a token the session no longer holds ends nothing", async () => {
  // The person signed out and in again while the request was out: the API
  // refused the old token, not the session that holds the new one.
  for (const refusal of [json(401, REFUSAL.invalid), json(403, REFUSAL.anonymous)]) {
    fake.reset();
    fake.signIn(SESSION_A);
    stubFetch(() => {
      fake.signIn(SESSION_B);
      return refusal.clone();
    });

    const { error } = await api.GET("/tracks");

    expect(error).toBeDefined();
    expect(fake.log).toEqual([]);
    expect(fake.session).toEqual(SESSION_B);
  }
});

test("an expired token the session has replaced is retried without a refresh", async () => {
  // A refresh for an earlier refusal already gave the session a new token.
  stubFetch((calls) => {
    if (calls.length > 1) return json(200, []);
    fake.signIn({ ...SESSION_A, access_token: "token-a-later" });
    return json(401, REFUSAL.expired);
  });

  const { error } = await api.GET("/tracks");

  expect(error).toBeUndefined();
  expect(fake.log).toEqual([]);
  expect(seen.map((call) => call.authorization)).toEqual([
    "Bearer token-a",
    "Bearer token-a-later",
  ]);
});

test("the Supabase client is made from the project's URL and key", async () => {
  stubFetch(() => json(200, []));
  await api.GET("/tracks");

  // Kept in storage and refreshed before it expires; the URL is not read for a
  // session, since the app's own query string carries the track (spec 094).
  expect(fake.created).toEqual([
    [
      "https://abc.supabase.co",
      "sb_publishable_synthetic",
      { auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: false } },
    ],
  ]);
});

test("a refresh the server refuses ends the session without a retry", async () => {
  // supabase-js removes the session itself, and fires the sign-out, before
  // refreshSession returns, so the app has nothing more to sign out of.
  fake.refreshTo = null;
  stubFetch(() => json(401, REFUSAL.expired));

  await api.GET("/tracks");

  expect(seen).toHaveLength(1);
  expect(fake.log).toEqual(["refreshSession"]);
});

test("a refresh that gets no answer ends the session without a retry", async () => {
  fake.refreshUnreachable = true;
  stubFetch(() => json(401, REFUSAL.expired));

  await api.GET("/tracks");

  expect(seen).toHaveLength(1);
  expect(fake.log).toEqual(["refreshSession", "signOut:local"]);
});

test("a retried write carries its body again", async () => {
  // A request's body can be read once. The retry is the copy made before the
  // first attempt, so the second attempt reads its own.
  stubFetch((calls) => (calls.length === 1 ? json(401, REFUSAL.expired) : json(200, {})));

  await api.POST("/tracker/{selector}/status", {
    params: { path: { selector: "1" }, query: {} },
    body: { verb: "shortlist", reason: null },
  });

  expect(seen).toHaveLength(2);
  expect(seen.map((call) => call.body)).toEqual([
    '{"verb":"shortlist","reason":null}',
    '{"verb":"shortlist","reason":null}',
  ]);
  expect(seen[1]?.authorization).toBe("Bearer refreshed-token");
});

test("a retry that gets no answer says so", async () => {
  let attempts = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(() => {
      attempts += 1;
      return attempts === 1
        ? Promise.resolve(json(401, REFUSAL.expired))
        : Promise.reject(new TypeError("Failed to fetch"));
    }),
  );

  await expect(api.GET("/tracks")).rejects.toThrow(UNREACHABLE);
});

test("three concurrent refusals sign the person out once", async () => {
  stubFetch(() => json(401, REFUSAL.invalid));

  await Promise.all([api.GET("/tracks"), api.GET("/tracks/kinds"), api.GET("/ops/schedule")]);

  expect(seen).toHaveLength(3);
  expect(fake.log).toEqual(["signOut:local"]);
});

test("a refused token is never retried", async () => {
  for (const [status, refusal] of [
    [401, REFUSAL.invalid],
    [401, REFUSAL.missing],
    [403, REFUSAL.anonymous],
  ] as const) {
    fake.reset();
    fake.signIn(SESSION_A);
    stubFetch(() => json(status, refusal));
    await api.GET("/tracks");
    expect(seen, refusal.reason).toHaveLength(1);
    expect(fake.log, refusal.reason).toEqual(["signOut:local"]);
  }
});

test("unavailable keys never sign anyone out", async () => {
  stubFetch(() => json(503, REFUSAL.keys));

  const { error } = await api.GET("/tracks");

  expect(error).toEqual(REFUSAL.keys);
  expect(fake.log).toEqual([]);
  expect(seen).toHaveLength(1);
});

test("a request that gets no answer says so", async () => {
  // An unhandled server error reaches the browser as a failed request, not a
  // status: it carries no CORS headers (spec 134).
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))),
  );
  await expect(api.GET("/tracks")).rejects.toThrow(UNREACHABLE);
  expect(fake.log).toEqual([]);

  // An aborted request is the caller's own doing and is left as it is.
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.reject(new DOMException("aborted", "AbortError"))),
  );
  await expect(api.GET("/tracks")).rejects.toMatchObject({ name: "AbortError" });
});

test("a reconnected stream carries the token as it is then", async () => {
  stubFetch(() => json(200, []));
  const streams = installRunStreams();

  for (const [index, next] of [SESSION_A, { ...SESSION_A, access_token: "token-a2" }].entries()) {
    fake.signIn(next);
    const read = (async () => {
      for await (const message of readRunEvents("r1", {
        lastEventId: 3,
        signal: new AbortController().signal,
      })) {
        void message;
      }
    })();
    await vi.waitFor(() => {
      expect(streams.connections).toHaveLength(index + 1);
    });
    streams.last.end();
    await read;
  }

  expect(streams.connections.map((connection) => connection.authorization)).toEqual([
    "Bearer token-a",
    "Bearer token-a2",
  ]);
  expect(streams.last.lastEventId).toBe("3");
});
