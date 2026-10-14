import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { stubApi } from "../../shared/testing/stubApi";
import type { ApiReply } from "../../shared/testing/stubApi";
import { TelegramForm } from "./TelegramForm";
import type { TelegramStatus } from "./TelegramForm";

// Synthetic: shaped like a bot token and a chat id, valid for nothing.
const BOT_TOKEN = "123456789:synthetic-bot-token-abcdef";
const CHAT_ID = "-1009876543210";

const NOT_SET: TelegramStatus = { configured: false, updated_at: null };
const SET: TelegramStatus = { configured: true, updated_at: "2026-10-14 10:00:00" };

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

async function fillAndSave(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  await user.type(screen.getByLabelText("Bot token"), BOT_TOKEN);
  await user.type(screen.getByLabelText("Chat id"), CHAT_ID);
  await user.click(screen.getByRole("button", { name: "Save Telegram settings" }));
}

function expectNoTrace(calls: { url: string }[]): void {
  const everywhere = [
    document.body.textContent,
    ...calls.map((call) => call.url),
    ...Object.keys(localStorage).map((key) => `${key}=${localStorage.getItem(key) ?? ""}`),
    JSON.stringify(consoleCalls),
  ].join("\n");
  expect(everywhere).not.toContain(BOT_TOKEN);
  expect(everywhere).not.toContain(CHAT_ID);
}

test("the Telegram form treats the chat id as a secret", async () => {
  const changes: TelegramStatus[] = [];
  const calls = stubApi((call): ApiReply => {
    if (call.method === "PUT" && call.path === "/api/notifications/telegram") {
      return { status: 200, body: SET };
    }
    return undefined;
  });
  const user = userEvent.setup();
  render(
    <TelegramForm
      status={NOT_SET}
      onChanged={(next) => {
        changes.push(next);
      }}
    />,
  );

  // Both fields are password inputs with autocomplete off, the chat id too.
  for (const label of ["Bot token", "Chat id"]) {
    const input = screen.getByLabelText<HTMLInputElement>(label);
    expect(input.type, label).toBe("password");
    expect(input.autocomplete, label).toBe("off");
  }
  // The person is told to message the bot first, before the fields.
  expect(screen.getByText(/Send your bot one message first/)).toBeDefined();

  await fillAndSave(user);
  await waitFor(() => {
    expect(changes).toEqual([SET]);
  });
  expect(calls.find((call) => call.method === "PUT")?.body).toEqual({
    bot_token: BOT_TOKEN,
    chat_id: CHAT_ID,
  });
  expect(screen.getByLabelText<HTMLInputElement>("Bot token").value).toBe("");
  expect(screen.getByLabelText<HTMLInputElement>("Chat id").value).toBe("");
  expectNoTrace(calls);
});

test("the Telegram form reads the refusals these routes send", async () => {
  const refusals: { name: string; status: number; body: unknown; shown: string }[] = [
    {
      name: "a rejected token",
      status: 400,
      body: { reason: "token_rejected", detail: "Telegram did not accept that bot token." },
      shown: "Telegram did not accept that bot token.",
    },
    {
      name: "a chat the bot cannot write to",
      status: 400,
      body: { reason: "chat_unreachable", detail: "The bot cannot write to that chat." },
      shown: "The bot cannot write to that chat.",
    },
    {
      name: "Telegram not answering",
      status: 502,
      body: { reason: "telegram_unavailable", detail: "Telegram could not be reached." },
      shown: "Telegram could not be reached.",
    },
    {
      name: "a body the schema refuses",
      status: 422,
      body: { detail: [{ field: "chat_id", problem: "must be a string" }] },
      shown: "chat_id: must be a string",
    },
  ];
  for (const { name, status, body, shown } of refusals) {
    const calls = stubApi((call): ApiReply =>
      call.method === "PUT" ? { status, body } : undefined,
    );
    const user = userEvent.setup();
    render(<TelegramForm status={NOT_SET} onChanged={() => undefined} />);

    await fillAndSave(user);

    expect((await screen.findByRole("alert")).textContent, name).toBe(shown);
    // Cleared after any answer, a refusal included.
    expect(screen.getByLabelText<HTMLInputElement>("Bot token").value, name).toBe("");
    expect(screen.getByLabelText<HTMLInputElement>("Chat id").value, name).toBe("");
    expectNoTrace(calls);
    cleanup();
  }
});

test("clearing the Telegram settings asks the API to forget them", async () => {
  const changes: TelegramStatus[] = [];
  stubApi((call): ApiReply =>
    call.method === "DELETE" && call.path === "/api/notifications/telegram"
      ? { status: 200, body: NOT_SET }
      : undefined,
  );
  const user = userEvent.setup();
  render(
    <TelegramForm
      status={SET}
      onChanged={(next) => {
        changes.push(next);
      }}
    />,
  );

  await user.click(screen.getByRole("button", { name: "Clear Telegram settings" }));
  await waitFor(() => {
    expect(changes).toEqual([NOT_SET]);
  });
});

test("a save that gets no answer says so, clears the secrets and stays usable", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))),
  );
  const user = userEvent.setup();
  render(<TelegramForm status={SET} onChanged={() => undefined} />);

  await fillAndSave(user);

  expect((await screen.findByRole("alert")).textContent).toBe("Failed to fetch");
  expect(screen.getByLabelText<HTMLInputElement>("Bot token").value).toBe("");
  expect(screen.getByLabelText<HTMLInputElement>("Chat id").value).toBe("");
  expect(screen.getByRole("button", { name: "Save Telegram settings" })).toHaveProperty(
    "disabled",
    false,
  );

  await user.click(screen.getByRole("button", { name: "Clear Telegram settings" }));
  await waitFor(() => {
    expect(screen.getByRole("button", { name: "Clear Telegram settings" })).toHaveProperty(
      "disabled",
      false,
    );
  });
  expect(document.body.textContent).not.toContain(BOT_TOKEN);
  expect(document.body.textContent).not.toContain(CHAT_ID);
});
