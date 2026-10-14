import { useEffect, useSyncExternalStore } from "react";

import type { Session, SupabaseClient } from "@supabase/supabase-js";

import { appMode } from "../config/appMode";

/**
 * The person's Supabase session, hosted mode only (spec 114).
 *
 * `supabase-js` is loaded by a dynamic import on first use, so the local
 * bundle neither loads nor initialises it (Open decision 7). The email address
 * a person types reaches `requestCode` and `verifyCode` as an argument and
 * goes no further than Supabase: it is never logged, stored by this code, or
 * put in a URL.
 */

/** Why a session ended. `signed-out` is the person's own act and says nothing. */
export type EndReason = "expired" | "invalid" | "refused" | "signed-out";

/** What the sign-in page says about a session that ended under the person. */
export type SessionNotice = Exclude<EndReason, "signed-out"> | null;

export type SessionState =
  | { readonly status: "loading" }
  | { readonly status: "signed-out"; readonly notice: SessionNotice }
  | { readonly status: "signed-in"; readonly userId: string };

export type CodeRequest = "sent" | "rate-limited" | "unreachable";
export type CodeCheck = "ok" | "refused" | "unreachable";

let state: SessionState = { status: "loading" };
const listeners = new Set<() => void>();
// Why the session is ending, held until the sign-out event arrives to say it.
let pendingNotice: SessionNotice = null;

// A state is a few strings, so equal states compare by their text: a token
// refresh, which changes nothing the app shows, must not re-render it.
function set(next: SessionState): void {
  if (JSON.stringify(state) === JSON.stringify(next)) return;
  state = next;
  for (const listener of listeners) listener();
}

function apply(session: Session | null): void {
  if (session === null) {
    set({ status: "signed-out", notice: pendingNotice });
    return;
  }
  pendingNotice = null;
  set({ status: "signed-in", userId: session.user.id });
}

let clientPromise: Promise<SupabaseClient> | null = null;

function client(): Promise<SupabaseClient> {
  clientPromise ??= (async () => {
    const mode = appMode();
    if (mode.kind !== "hosted") throw new Error("there is no sign-in in local mode");
    const { createClient } = await import("@supabase/supabase-js");
    const created = createClient(mode.supabaseUrl, mode.publishableKey, {
      // The documented defaults, written out: the session is kept in
      // localStorage and refreshed before it expires. A code sign-in puts
      // nothing in the URL, and the app's own query string carries the
      // selected track (spec 094), so the URL is not read for a session.
      auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: false },
    });
    created.auth.onAuthStateChange((_event, session) => {
      apply(session);
    });
    return created;
  })();
  // A failed load is not kept: the next call tries again.
  clientPromise.catch(() => {
    clientPromise = null;
  });
  return clientPromise;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** The session for components: loading until Supabase has read its storage. */
export function useSession(): SessionState {
  useEffect(() => {
    // Creates the client, which reports the stored session as its first event.
    client().catch(() => {
      set({ status: "signed-out", notice: null });
    });
  }, []);
  return useSyncExternalStore(subscribe, () => state);
}

/** Who is signed in, and the access token to send for them. */
export interface ActiveSession {
  readonly userId: string;
  readonly token: string;
}

/** The session as it is now, its token refreshed first if about to expire. */
export async function activeSession(): Promise<ActiveSession | null> {
  try {
    const { data } = await (await client()).auth.getSession();
    const session = data.session;
    return session === null ? null : { userId: session.user.id, token: session.access_token };
  } catch {
    return null;
  }
}

/** The access token to send, refreshed first if it is about to expire. */
export async function accessToken(): Promise<string | null> {
  return (await activeSession())?.token ?? null;
}

// Refresh tokens are single use, so concurrent callers share one refresh.
let refreshing: Promise<string | null> | null = null;

/**
 * A new access token from the refresh token, or null when there is none.
 *
 * Called when the API said the token expired. If the refresh is refused,
 * supabase-js removes the session and fires `SIGNED_OUT` itself, before this
 * returns, so the notice for that sign-out is set first.
 */
export function refreshSession(): Promise<string | null> {
  refreshing ??= (async () => {
    pendingNotice = "expired";
    try {
      const { data, error } = await (await client()).auth.refreshSession();
      const token = error === null ? (data.session?.access_token ?? null) : null;
      if (token !== null) pendingNotice = null;
      return token;
    } catch {
      return null;
    }
  })().finally(() => {
    refreshing = null;
  });
  return refreshing;
}

let ending: Promise<void> | null = null;

/**
 * End this browser's session: `signOut({ scope: "local" })` removes it from
 * storage and fires `SIGNED_OUT` (Open decision 4). Concurrent callers, and a
 * caller arriving after the session is gone, do nothing more, so three
 * refused requests sign the person out once.
 */
export function endSession(reason: EndReason): Promise<void> {
  if (ending !== null) return ending;
  if (state.status === "signed-out") return Promise.resolve();
  const notice: SessionNotice = reason === "signed-out" ? null : reason;
  pendingNotice = notice;
  ending = (async () => {
    try {
      await (await client()).auth.signOut({ scope: "local" });
    } catch {
      // supabase-js clears its stored session even when the server call
      // fails; the app goes to the sign-in page either way.
    }
    set({ status: "signed-out", notice });
  })().finally(() => {
    ending = null;
  });
  return ending;
}

interface RefusedAuthError {
  readonly name?: string;
  readonly status?: number;
  readonly code?: string;
}

function rateLimited(error: RefusedAuthError): boolean {
  return error.status === 429 || (error.code ?? "").startsWith("over_");
}

// supabase-js reports a request that never got an answer as an
// `AuthRetryableFetchError` with status 0.
function unreachable(error: RefusedAuthError): boolean {
  return error.name === "AuthRetryableFetchError" || error.status === 0;
}

/**
 * Ask for a sign-in code. `shouldCreateUser: false` stops this form from
 * creating accounts, which are made by invitation (specs 102 and 111). Every
 * answer but a rate limit or a network failure is `sent`: the page does not
 * say whether the address has an account.
 */
export async function requestCode(email: string): Promise<CodeRequest> {
  try {
    const { error } = await (
      await client()
    ).auth.signInWithOtp({ email, options: { shouldCreateUser: false } });
    if (error === null) return "sent";
    if (rateLimited(error)) return "rate-limited";
    return unreachable(error) ? "unreachable" : "sent";
  } catch {
    return "unreachable";
  }
}

/** Check the emailed code. On success Supabase fires `SIGNED_IN`. */
export async function verifyCode(email: string, code: string): Promise<CodeCheck> {
  try {
    const { error } = await (await client()).auth.verifyOtp({ email, token: code, type: "email" });
    if (error === null) return "ok";
    return unreachable(error) ? "unreachable" : "refused";
  } catch {
    return "unreachable";
  }
}
