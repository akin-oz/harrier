import { useId, useState } from "react";
import type { SyntheticEvent } from "react";

import type { components } from "@harrier/contract";

import { api } from "../../shared/api/client";
import { refusalMessage } from "../../shared/api/refusal";
import "./notifications.css";

export type TelegramStatus = components["schemas"]["TelegramSettingsOut"];

/**
 * The owner's Telegram bot (spec 114, amendment of 2026-10-11; routes in
 * spec 109).
 *
 * The bot token and the chat id are both secrets (decision D7): password
 * inputs with autocomplete off, held in this component's state only, sent to
 * the save request and nowhere else, and cleared after any answer. A refusal
 * is shown in the API's words: `detail` is a fixed sentence per `reason`
 * (400 and 502), or the field and the rule a 422 names, and never a value.
 */
export function TelegramForm({
  status,
  onChanged,
}: {
  status: TelegramStatus;
  onChanged: (next: TelegramStatus) => void;
}) {
  const id = useId();
  const [botToken, setBotToken] = useState("");
  const [chatId, setChatId] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function save(event: SyntheticEvent): Promise<void> {
    event.preventDefault();
    setBusy(true);
    setProblem(null);
    const { data, error } = await api.PUT("/notifications/telegram", {
      body: { bot_token: botToken, chat_id: chatId },
    });
    setBusy(false);
    // Whatever the answer, the secrets are not kept in the page.
    setBotToken("");
    setChatId("");
    if (error !== undefined) {
      setProblem(refusalMessage(error, "Telegram settings were refused"));
      return;
    }
    onChanged(data);
  }

  async function clear(): Promise<void> {
    setBusy(true);
    setProblem(null);
    const { data, error } = await api.DELETE("/notifications/telegram");
    setBusy(false);
    if (error !== undefined) {
      setProblem(refusalMessage(error, "Telegram settings were not cleared"));
      return;
    }
    onChanged(data);
  }

  return (
    <form
      className="telegram-form"
      aria-label="Telegram notifications"
      onSubmit={(event) => {
        void save(event);
      }}
    >
      <p className="telegram-form__state">
        {status.configured
          ? `Configured, updated ${status.updated_at ?? "at an unknown time"}`
          : "Not configured"}
      </p>
      <p className="telegram-form__note">
        Send your bot one message first. Saving checks the bot, then sends one confirmation to the
        chat, which only works once the chat has written to the bot.
      </p>
      <div className="telegram-form__field">
        <label htmlFor={`${id}-token`}>Bot token</label>
        <input
          id={`${id}-token`}
          type="password"
          autoComplete="off"
          required
          value={botToken}
          onChange={(event) => {
            setBotToken(event.target.value);
          }}
        />
      </div>
      <div className="telegram-form__field">
        <label htmlFor={`${id}-chat`}>Chat id</label>
        <input
          id={`${id}-chat`}
          type="password"
          autoComplete="off"
          required
          value={chatId}
          onChange={(event) => {
            setChatId(event.target.value);
          }}
        />
      </div>
      <div className="telegram-form__actions">
        <button type="submit" disabled={busy}>
          Save Telegram settings
        </button>
        {status.configured && (
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              void clear();
            }}
          >
            Clear Telegram settings
          </button>
        )}
      </div>
      {problem !== null && (
        <p role="alert" className="telegram-form__problem">
          {problem}
        </p>
      )}
    </form>
  );
}
