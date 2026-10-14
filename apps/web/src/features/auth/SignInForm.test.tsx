import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

vi.hoisted(() => {
  vi.stubEnv("VITE_HARRIER_AUTH", "supabase");
  vi.stubEnv("VITE_SUPABASE_URL", "https://abc.supabase.co");
  vi.stubEnv("VITE_SUPABASE_PUBLISHABLE_KEY", "sb_publishable_synthetic");
  vi.stubEnv("VITE_HARRIER_API_URL", "https://api.example.com");
});

vi.mock("@supabase/supabase-js", async () => ({
  createClient: (await import("../../shared/testing/fakeSupabase")).fake.createClient,
}));

import { fake } from "../../shared/testing/fakeSupabase";
import { CODE_SENT, RATE_LIMITED, SignInForm, UNREACHABLE, WRONG_CODE } from "./SignInForm";

// Synthetic: an address on a reserved domain, belonging to no one.
const EMAIL = "person@example.invalid";

const consoleCalls: unknown[][] = [];
let harrierRequests: string[] = [];

beforeEach(() => {
  fake.reset();
  fake.session = null;
  consoleCalls.length = 0;
  harrierRequests = [];
  for (const method of ["log", "info", "warn", "error", "debug"] as const) {
    vi.spyOn(console, method).mockImplementation((...args: unknown[]) => {
      consoleCalls.push(args);
    });
  }
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      harrierRequests.push(input instanceof Request ? input.url : String(input));
      return Promise.resolve(new Response("{}"));
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
});

async function askForCode(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  await user.type(screen.getByLabelText("Email address"), EMAIL);
  await user.click(screen.getByRole("button", { name: "Send a sign-in code" }));
}

test("the code request never says whether an account exists", async () => {
  const user = userEvent.setup();

  // An unknown address: Supabase refuses to create the user.
  fake.otpError = {
    name: "AuthApiError",
    status: 422,
    code: "otp_disabled",
    message: "Signups not allowed for otp",
  };
  const unknown = render(<SignInForm />);
  await askForCode(user);
  const forUnknown = (await screen.findByRole("status")).textContent;
  unknown.unmount();

  // A known address.
  fake.otpError = null;
  render(<SignInForm />);
  await askForCode(user);
  const forKnown = (await screen.findByRole("status")).textContent;

  expect(forUnknown).toBe(CODE_SENT);
  expect(forKnown).toBe(CODE_SENT);
  // The form never creates an account.
  expect(fake.otpRequests.map((request) => request.shouldCreateUser)).toEqual([false, false]);
  expect(fake.otpRequests[0]?.email).toBe(EMAIL);
});

test("a rate limit and a network failure each say so", async () => {
  const user = userEvent.setup();
  fake.otpError = {
    name: "AuthApiError",
    status: 429,
    code: "over_email_send_rate_limit",
    message: "email rate limit exceeded",
  };
  const limited = render(<SignInForm />);
  await askForCode(user);
  expect((await screen.findByRole("status")).textContent).toBe(RATE_LIMITED);
  // Still on the address step: the person can ask again later.
  expect(screen.getByLabelText("Email address")).toBeDefined();
  limited.unmount();

  fake.otpError = null;
  fake.otpThrows = true;
  render(<SignInForm />);
  await askForCode(user);
  expect((await screen.findByRole("status")).textContent).toBe(UNREACHABLE);
});

test("a wrong code shows the refusal sentence", async () => {
  const user = userEvent.setup();
  render(<SignInForm />);
  await askForCode(user);
  await screen.findByLabelText("Sign-in code");

  fake.verifyError = {
    name: "AuthApiError",
    status: 403,
    code: "otp_expired",
    message: "Token has expired or is invalid",
  };
  await user.type(screen.getByLabelText("Sign-in code"), "000000");
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  await waitFor(() => {
    expect(screen.getByRole("status").textContent).toBe(WRONG_CODE);
  });
  expect(fake.verifications).toEqual([{ email: EMAIL, token: "000000", type: "email" }]);
  // A new code can be asked for.
  await user.click(screen.getByRole("button", { name: "Ask for a new code" }));
  expect(screen.getByLabelText("Email address")).toBeDefined();
});

test("the address reaches Supabase and nothing else", async () => {
  const user = userEvent.setup();
  render(<SignInForm />);
  await askForCode(user);
  await screen.findByLabelText("Sign-in code");
  await user.type(screen.getByLabelText("Sign-in code"), "123456");
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  await waitFor(() => {
    expect(fake.verifications).toHaveLength(1);
  });

  // Not in a request to the harrier API, not in storage this code wrote, not
  // in anything it printed.
  expect(harrierRequests.some((url) => url.includes("person"))).toBe(false);
  const stored = Object.keys(localStorage).map(
    (key) => `${key}=${localStorage.getItem(key) ?? ""}`,
  );
  expect(stored.join("\n")).not.toContain("person@");
  expect(JSON.stringify(consoleCalls)).not.toContain("person@");
  expect(window.location.href).not.toContain("person");
});
