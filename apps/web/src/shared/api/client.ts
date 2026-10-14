import createClient from "openapi-fetch";
import type { Middleware } from "openapi-fetch";

import type { components, paths } from "@harrier/contract";

import { accessToken, activeSession, endSession, refreshSession } from "../auth/session";
import type { ActiveSession, EndReason } from "../auth/session";
import { appMode, configError } from "../config/appMode";
import type { AppMode } from "../config/mode";

// The only way the web app talks to the API: typed by the generated contract
// (ADR-005). Two modes, fixed when the bundle is built (spec 114):
//
// - Local: the API shares the page's origin, the Vite dev server proxies /api
//   to the FastAPI service, and a state-changing request carries the local
//   token of spec 035.
// - Hosted: the API is at VITE_HARRIER_API_URL, and every request carries the
//   signed-in person's Supabase access token as a bearer token. The local
//   token is never sent and `GET /session` is never requested.
//
// The base URL is absolute (resolved against the page origin) because Node's
// Request, used by vitest/jsdom, rejects relative URLs; fetch resolves at
// request time (not client creation) so test stubs apply.
//
// A build whose configuration is refused renders a page that says so and sends
// nothing, so it gets the local shape here only so this module can exist.
const mode: AppMode = configError() === null ? appMode() : { kind: "local" };

const baseUrl =
  mode.kind === "hosted" ? mode.apiUrl : new URL("/api", globalThis.location.href).toString();

export const TOKEN_HEADER = "X-Harrier-Token";

const MUTATING = new Set(["POST", "PUT", "PATCH", "DELETE"]);

// The reads that carry the token. Tracker reads do not, and these differ
// deliberately. An artifact is a generated resume, cover letter or offer
// evaluation, which is the densest personal content the system holds
// (spec 047). An outreach read is a named human being who is not the
// operator, which is the only content here about someone who never chose to
// use this tool (spec 048). The API requires the token on both. A brief is
// the operator's own notes about an application, and the data check names
// contacts, so both are read with it too (spec 095).
//
// Configuration is the watchlist, the searches and the hold list, which
// describe the operator's own search (spec 097 closed spec 023's open item).
// The Settings reads name archives, model metadata and profile documents,
// which describe the operator's own data (spec 096). The export downloads
// carry the operator's rows and contact identities, and take the token in
// this header so it never sits in a URL (spec 096).
const TOKENED_READS = [
  /\/apply\/[^/]+\/artifacts(\/|$)/,
  /\/apply\/[^/]+\/brief$/,
  /\/ops\/check$/,
  /\/outreach\//,
  /\/config(\/|$)/,
  /\/settings\//,
  /\/ops\/export\//,
  // Profile document names, by Akin's decision of 2026-10-10 (spec 096).
  /\/ops\/profile$/,
];

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

const localMiddleware: Middleware = {
  async onRequest({ request }) {
    // Only where it is required. Adding it to every read would send it for no
    // gain and make it that much easier to leak.
    if (!needsToken(request)) return undefined;
    const token = await localToken();
    if (token) request.headers.set(TOKEN_HEADER, token);
    return request;
  },
};

type AuthRefusal = components["schemas"]["AuthRefusalOut"];
type AuthReason = AuthRefusal["reason"];

// What each refusal that ends the session ends it as. Keyed by the contract's
// own reasons, so one the API adds or drops fails the type check here (spec
// 114). `expired` is handled apart, because a refresh can still save it, and
// `keys_unavailable` is the API's trouble, not the person's session.
const ENDS_SESSION: Record<Exclude<AuthReason, "expired" | "keys_unavailable">, EndReason> = {
  missing: "invalid",
  invalid: "invalid",
  anonymous: "refused",
};

export const UNREACHABLE =
  "The harrier API did not answer. It may be down, or it may have failed on this request.";

async function reasonOf(response: Response): Promise<AuthReason | null> {
  try {
    const body = (await response.clone().json()) as Partial<AuthRefusal>;
    return body.reason ?? null;
  } catch {
    return null;
  }
}

function endingFor(reason: AuthReason | null): EndReason {
  if (reason === "expired") return "expired";
  if (reason === null || reason === "keys_unavailable") return "invalid";
  return ENDS_SESSION[reason];
}

// A request that got no answer rejects with a TypeError. An aborted one
// rejects with an AbortError and is not this.
function isNoAnswer(error: unknown): boolean {
  return error instanceof Error && error.name === "TypeError";
}

interface Sent {
  // A copy of the request as it left, kept for the one retry after a refresh.
  readonly copy: Request;
  // The session it was sent for, or null when it went without a token.
  readonly session: ActiveSession | null;
}

function hostedMiddleware(): Middleware {
  const sent = new WeakMap<Request, Sent>();

  // A refusal ends only the session whose token was refused. A session that
  // has moved on since the request left (refreshed, or signed out and in
  // again) is not the one the API refused (CodeRabbit on PR #328).
  async function refuse(response: Response, token: string | null): Promise<void> {
    const reason = await reasonOf(response);
    if (response.status === 403 && reason !== "anonymous") return;
    if ((await accessToken()) !== token) return;
    await endSession(endingFor(reason));
  }

  // The token to retry an expired request with: the session's own, when a
  // refresh for an earlier refusal already replaced it, or a new one. Null
  // when the request is not to be retried. A request is only ever retried as
  // the person who sent it: after someone else signs in, the refusal goes
  // back to its caller (CodeRabbit on PR #328).
  async function retryToken(first: ActiveSession): Promise<string | null> {
    const now = await activeSession();
    if (now === null || now.userId !== first.userId) return null;
    if (now.token !== first.token) return now.token;
    const token = await refreshSession();
    if (token === null) {
      await endSession("expired");
      return null;
    }
    return (await activeSession())?.userId === first.userId ? token : null;
  }

  return {
    async onRequest({ request }) {
      const session = await activeSession();
      // With no session the request goes without the header, and the API's
      // 401 `missing` ends the session below.
      if (session !== null) request.headers.set("Authorization", `Bearer ${session.token}`);
      sent.set(request, { copy: request.clone(), session });
      return request;
    },
    async onResponse({ request, response }) {
      const first = sent.get(request);
      const firstToken = first?.session?.token ?? null;
      if (response.status === 401) {
        if ((await reasonOf(response)) !== "expired" || first?.session == null) {
          await refuse(response, firstToken);
          return undefined;
        }
        const token = await retryToken(first.session);
        if (token === null) return undefined;
        const retry = first.copy;
        retry.headers.set("Authorization", `Bearer ${token}`);
        // openapi-fetch does not pass an error thrown here to `onError`, so the
        // retry gives the no-answer sentence itself.
        const second = await globalThis.fetch(retry).catch((error: unknown) => {
          throw isNoAnswer(error) ? new Error(UNREACHABLE) : error;
        });
        // A second refusal is final: no further retry.
        if (second.status === 401 || second.status === 403) await refuse(second, token);
        return second;
      }
      if (response.status === 403) await refuse(response, firstToken);
      return undefined;
    },
    onError({ error }) {
      // A request that got no answer rejects with a TypeError. That includes
      // the 500 of an unhandled server error, which carries no CORS headers
      // and so reaches the browser as a failed request, not as a status
      // (spec 134). An aborted stream rejects with an AbortError and is left
      // alone.
      return isNoAnswer(error) ? new Error(UNREACHABLE) : undefined;
    },
  };
}

export const api = createClient<paths>({
  baseUrl,
  fetch: (input) => globalThis.fetch(input),
});

api.use(mode.kind === "hosted" ? hostedMiddleware() : localMiddleware);

export type ApiClient = typeof api;
