import { vi } from "vitest";

/**
 * Supabase's browser client, faked at its boundary (spec 114). Only tests
 * import this: no network, no real project, and every token is synthetic.
 *
 * A test file mocks the package with
 * `vi.mock("@supabase/supabase-js", async () => ({ createClient: (await import("...fakeSupabase")).fake.createClient }))`
 * and drives the session through `fake`.
 */

export interface FakeSession {
  access_token: string;
  user: { id: string };
}

export interface FakeAuthError {
  name: string;
  status?: number;
  code?: string;
  message: string;
}

type Listener = (event: string, session: FakeSession | null) => void;

export class FakeSupabase {
  session: FakeSession | null = null;
  /** The token the next refresh yields; null makes the refresh fail. */
  refreshTo: string | null = "refreshed-token";
  /** What the next code request answers instead of success. */
  otpError: FakeAuthError | null = null;
  otpThrows = false;
  /** What the next code check answers instead of success. */
  verifyError: FakeAuthError | null = null;
  /** The session a verified code signs in. */
  verifiedSession: FakeSession = { access_token: "verified-token", user: { id: "user-verified" } };
  /** Every call that matters, in order. */
  log: string[] = [];
  otpRequests: { email: string; shouldCreateUser: boolean | undefined }[] = [];
  verifications: { email: string; token: string; type: string }[] = [];
  private listeners = new Set<Listener>();

  /** The arguments of every `createClient` call: url, key and options. Kept
   * across `reset`, since a module makes its client once. */
  readonly created: unknown[][] = [];

  readonly createClient = vi.fn((...args: unknown[]) => {
    this.created.push(args);
    return this.client;
  });

  private emit(event: string, session: FakeSession | null): void {
    for (const listener of [...this.listeners]) listener(event, session);
  }

  /** Sign in as someone, as another tab or a verified code would. */
  signIn(session: FakeSession): void {
    this.session = session;
    this.emit("SIGNED_IN", session);
  }

  /** The session ends from outside, as another tab's sign-out would. */
  signOutElsewhere(): void {
    this.session = null;
    this.emit("SIGNED_OUT", null);
  }

  readonly auth = {
    getSession: vi.fn(() => Promise.resolve({ data: { session: this.session }, error: null })),
    refreshSession: vi.fn(() => {
      this.log.push("refreshSession");
      if (this.refreshTo === null || this.session === null) {
        return Promise.resolve({
          data: { session: null },
          error: { name: "AuthApiError", status: 400, code: "refresh_token_already_used" },
        });
      }
      this.session = { ...this.session, access_token: this.refreshTo };
      this.emit("TOKEN_REFRESHED", this.session);
      return Promise.resolve({ data: { session: this.session }, error: null });
    }),
    signOut: vi.fn((options?: { scope?: string }) => {
      this.log.push(`signOut:${options?.scope ?? "global"}`);
      this.session = null;
      this.emit("SIGNED_OUT", null);
      return Promise.resolve({ error: null });
    }),
    signInWithOtp: vi.fn((args: { email: string; options?: { shouldCreateUser?: boolean } }) => {
      this.otpRequests.push({
        email: args.email,
        shouldCreateUser: args.options?.shouldCreateUser,
      });
      if (this.otpThrows) return Promise.reject(new TypeError("Failed to fetch"));
      return Promise.resolve({ data: {}, error: this.otpError });
    }),
    verifyOtp: vi.fn((args: { email: string; token: string; type: string }) => {
      this.verifications.push(args);
      if (this.verifyError !== null) return Promise.resolve({ data: {}, error: this.verifyError });
      this.session = this.verifiedSession;
      this.emit("SIGNED_IN", this.session);
      return Promise.resolve({ data: { session: this.session }, error: null });
    }),
    onAuthStateChange: vi.fn((listener: Listener) => {
      this.listeners.add(listener);
      // supabase-js reports the stored session as its first event.
      queueMicrotask(() => {
        listener("INITIAL_SESSION", this.session);
      });
      return {
        data: {
          subscription: {
            unsubscribe: () => {
              this.listeners.delete(listener);
            },
          },
        },
      };
    }),
  };

  readonly client = { auth: this.auth };

  /** Forget the calls and the settings, keeping the listeners a module made. */
  reset(): void {
    this.refreshTo = "refreshed-token";
    this.otpError = null;
    this.otpThrows = false;
    this.verifyError = null;
    this.log = [];
    this.otpRequests = [];
    this.verifications = [];
    this.createClient.mockClear();
    this.auth.getSession.mockClear();
    this.auth.refreshSession.mockClear();
    this.auth.signOut.mockClear();
    this.auth.signInWithOtp.mockClear();
    this.auth.verifyOtp.mockClear();
  }
}

export const fake = new FakeSupabase();

export const SESSION_A: FakeSession = { access_token: "token-a", user: { id: "user-a" } };
export const SESSION_B: FakeSession = { access_token: "token-b", user: { id: "user-b" } };
