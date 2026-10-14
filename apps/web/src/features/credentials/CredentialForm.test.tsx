import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { stubApi } from "../../shared/testing/stubApi";
import type { ApiReply } from "../../shared/testing/stubApi";
import { CredentialForm } from "./CredentialForm";
import type { CredentialStatus, FormKind } from "./CredentialForm";

// Synthetic values, none of them a key to anything.
const SECRET = "sk-synthetic-0000-secret";
const MODEL = "synthetic-model-1";

const consoleCalls: unknown[][] = [];

beforeEach(() => {
  consoleCalls.length = 0;
  for (const method of ["log", "info", "warn", "error", "debug"] as const) {
    vi.spyOn(console, method).mockImplementation((...args: unknown[]) => {
      consoleCalls.push(args);
    });
  }
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
});

const UNSET = (kind: FormKind): CredentialStatus => ({ kind, set: false, updated_at: null });
const SAVED = (kind: FormKind): CredentialStatus => ({
  kind,
  set: true,
  updated_at: "2026-10-14 10:00:00",
});

/** Nothing the person typed is anywhere it should not be. */
function expectNoTrace(calls: { url: string }[], values: string[]): void {
  const everywhere = [
    document.body.textContent,
    ...calls.map((call) => call.url),
    ...Object.keys(localStorage).map((key) => `${key}=${localStorage.getItem(key) ?? ""}`),
    JSON.stringify(consoleCalls),
  ].join("\n");
  for (const value of values) expect(everywhere).not.toContain(value);
}

test("a saved credential leaves no value in the page", async () => {
  const changes: CredentialStatus[] = [];
  const calls = stubApi((call): ApiReply => {
    if (call.method === "PUT" && call.path === "/api/credentials/ai") {
      return { status: 200, body: SAVED("ai") };
    }
    return undefined;
  });
  const user = userEvent.setup();
  render(
    <CredentialForm
      kind="ai"
      status={UNSET("ai")}
      onChanged={(next) => {
        changes.push(next);
      }}
    />,
  );

  await user.selectOptions(screen.getByLabelText("Provider"), "anthropic-api");
  await user.type(screen.getByLabelText("Key"), SECRET);
  await user.type(screen.getByLabelText("Model (optional)"), MODEL);
  await user.click(screen.getByRole("button", { name: "Save ai provider key" }));

  await waitFor(() => {
    expect(changes).toEqual([SAVED("ai")]);
  });
  // The form's own fields, and nothing else, went in the body.
  const put = calls.find((call) => call.method === "PUT");
  expect(put?.body).toEqual({ provider: "anthropic-api", key: SECRET, model: MODEL });
  // Every field is empty again, and the secret field is never a plain one.
  expect(screen.getByLabelText<HTMLInputElement>("Key").value).toBe("");
  expect(screen.getByLabelText<HTMLInputElement>("Model (optional)").value).toBe("");
  expect(screen.getByLabelText<HTMLInputElement>("Key").type).toBe("password");
  expect(screen.getByLabelText<HTMLInputElement>("Key").autocomplete).toBe("off");
  expectNoTrace(calls, [SECRET, MODEL]);
});

test("a refused credential shows its field and problem, never its value", async () => {
  const calls = stubApi((call): ApiReply => {
    if (call.method === "PUT") {
      return {
        status: 422,
        body: { detail: [{ field: "token", problem: "holds a control character" }] },
      };
    }
    return undefined;
  });
  const user = userEvent.setup();
  render(<CredentialForm kind="apify" status={UNSET("apify")} onChanged={() => undefined} />);

  await user.type(screen.getByLabelText("Token"), SECRET);
  await user.click(screen.getByRole("button", { name: "Save apify token" }));

  expect((await screen.findByRole("alert")).textContent).toBe("token: holds a control character");
  expect(calls.find((call) => call.method === "PUT")?.body).toEqual({ token: SECRET });
  expectNoTrace(calls, [SECRET]);
});

test("each kind sends its own fields", async () => {
  const calls = stubApi((call): ApiReply => {
    if (call.method === "PUT") return { status: 200, body: SAVED("hunter") };
    return undefined;
  });
  const user = userEvent.setup();
  render(<CredentialForm kind="hunter" status={UNSET("hunter")} onChanged={() => undefined} />);

  await user.type(screen.getByLabelText("Key"), SECRET);
  await user.click(screen.getByRole("button", { name: "Save hunter key" }));

  await waitFor(() => {
    expect(calls.some((call) => call.path === "/api/credentials/hunter")).toBe(true);
  });
  expect(calls.find((call) => call.method === "PUT")?.body).toEqual({ key: SECRET });
});

test("removing a credential asks first, then deletes it", async () => {
  const changes: CredentialStatus[] = [];
  const calls = stubApi((call): ApiReply => {
    if (call.method === "DELETE" && call.path === "/api/credentials/ai") {
      return { status: 200, body: UNSET("ai") };
    }
    return undefined;
  });
  const user = userEvent.setup();
  render(
    <CredentialForm
      kind="ai"
      status={SAVED("ai")}
      onChanged={(next) => {
        changes.push(next);
      }}
    />,
  );

  await user.click(screen.getByRole("button", { name: "Remove" }));
  // Nothing is deleted until the person confirms.
  expect(calls.some((call) => call.method === "DELETE")).toBe(false);
  await user.click(screen.getByRole("button", { name: "Keep it" }));
  expect(calls.some((call) => call.method === "DELETE")).toBe(false);

  await user.click(screen.getByRole("button", { name: "Remove" }));
  await user.click(screen.getByRole("button", { name: "Yes, remove" }));
  await waitFor(() => {
    expect(changes).toEqual([UNSET("ai")]);
  });
});

test("a save or a removal that gets no answer says so and leaves the form usable", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))),
  );
  const user = userEvent.setup();
  render(<CredentialForm kind="hunter" status={SAVED("hunter")} onChanged={() => undefined} />);

  await user.type(screen.getByLabelText("Key"), SECRET);
  await user.click(screen.getByRole("button", { name: "Save hunter key" }));
  expect((await screen.findByRole("alert")).textContent).toBe("Failed to fetch");
  // Not stuck: the button works again, without a reload.
  expect(screen.getByRole("button", { name: "Save hunter key" })).toHaveProperty("disabled", false);

  await user.click(screen.getByRole("button", { name: "Remove" }));
  await user.click(screen.getByRole("button", { name: "Yes, remove" }));
  await waitFor(() => {
    expect(screen.queryByRole("group")).toBeNull();
  });
  expect(screen.getByRole("alert").textContent).toBe("Failed to fetch");
  expect(screen.getByRole("button", { name: "Save hunter key" })).toHaveProperty("disabled", false);
  expect(document.body.textContent).not.toContain(SECRET);
});
