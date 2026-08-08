import createClient from "openapi-fetch";

import type { paths } from "@harrier/contract";

// The only way the web app talks to the API: typed by the generated contract
// (ADR-005). The Vite dev server proxies /api to the FastAPI service.
// The base URL is absolute (resolved against the page origin) because Node's
// Request, used by vitest/jsdom, rejects relative URLs; fetch resolves at
// request time (not client creation) so test stubs apply.
const baseUrl = new URL("/api", globalThis.location.href).toString();

export const api = createClient<paths>({
  baseUrl,
  fetch: (input) => globalThis.fetch(input),
});

export type ApiClient = typeof api;
