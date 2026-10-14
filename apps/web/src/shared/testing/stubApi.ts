import { vi } from "vitest";

/**
 * A fetch stub for component tests: records every request the generated
 * client makes and answers from a handler. Only tests import this.
 *
 * `/api/session` always answers with a token, so a test can assert that a
 * request carried it, which is the point of the client's token rules.
 */
export interface ApiCall {
  method: string;
  path: string;
  // The whole address, query string included, for a test that says a value
  // is in no URL.
  url: string;
  body: unknown;
  token: string | null;
}

export type ApiReply = { status: number; body: unknown } | undefined;

export const TEST_TOKEN = "test-token";

export function stubApi(handler: (call: ApiCall) => ApiReply): ApiCall[] {
  const calls: ApiCall[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(String(input), init);
      const path = new URL(request.url).pathname;
      const raw = request.method === "GET" ? "" : await request.clone().text();
      const call: ApiCall = {
        method: request.method,
        path,
        url: request.url,
        body: raw === "" ? null : (JSON.parse(raw) as unknown),
        token: request.headers.get("X-Harrier-Token"),
      };
      calls.push(call);
      const reply =
        path === "/api/session" ? { status: 200, body: { token: TEST_TOKEN } } : handler(call);
      // A string body is a file, such as a CSV download; anything else is JSON.
      if (typeof reply?.body === "string") {
        return new Response(reply.body, {
          status: reply.status,
          headers: { "Content-Type": "text/csv" },
        });
      }
      return new Response(JSON.stringify(reply?.body ?? { detail: `unstubbed ${path}` }), {
        status: reply?.status ?? 404,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return calls;
}
