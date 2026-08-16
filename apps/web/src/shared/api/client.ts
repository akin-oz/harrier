import createClient from "openapi-fetch";

import type { paths } from "@harrier/contract";

// The only way the web app talks to the API: typed by the generated contract
// (ADR-005). The Vite dev server proxies /api to the FastAPI service.
// The base URL is absolute (resolved against the page origin) because Node's
// Request, used by vitest/jsdom, rejects relative URLs; fetch resolves at
// request time (not client creation) so test stubs apply.
const baseUrl = new URL("/api", globalThis.location.href).toString();

export const TOKEN_HEADER = "X-Harrier-Token";

const MUTATING = new Set(["POST", "PUT", "PATCH", "DELETE"]);

// The reads that carry the token. Tracker reads do not, and these differ
// deliberately. An artifact is a generated resume, cover letter or offer
// evaluation, which is the densest personal content the system holds
// (spec 047). An outreach read is a named human being who is not the
// operator, which is the only content here about someone who never chose to
// use this tool (spec 048). The API requires the token on both.
const TOKENED_READS = [/\/apply\/[^/]+\/artifacts(\/|$)/, /\/outreach\//];

function needsToken(request: Request): boolean {
  if (MUTATING.has(request.method.toUpperCase())) return true;
  const path = new URL(request.url).pathname;
  return TOKENED_READS.some((pattern) => pattern.test(path));
}

// Fetched once and reused. The API refuses a state-changing request that does
// not carry it, which is what stops a page that merely happens to be open in
// the same browser from starting a run or rewriting configuration
// (spec 035). Read from a same-origin route: a cross-origin page can issue
// that request but cannot read its response.
let tokenRequest: Promise<string> | undefined;

async function localToken(): Promise<string> {
  tokenRequest ??= globalThis
    .fetch(`${baseUrl}/session`)
    .then((response) => (response.ok ? response.json() : { token: "" }))
    .then((body: { token?: string }) => body.token ?? "")
    .catch(() => "");
  const token = await tokenRequest;
  // A failed fetch must not poison the cache. Holding onto "" meant the API
  // refused every state-changing request from then on, and a reload was the
  // only cure (review finding on PR #39).
  if (!token) tokenRequest = undefined;
  return token;
}

export const api = createClient<paths>({
  baseUrl,
  fetch: (input) => globalThis.fetch(input),
});

api.use({
  async onRequest({ request }) {
    // Only where it is required. Adding it to every read would send it for no
    // gain and make it that much easier to leak.
    if (!needsToken(request)) return undefined;
    const token = await localToken();
    if (token) request.headers.set(TOKEN_HEADER, token);
    return request;
  },
});

export type ApiClient = typeof api;
