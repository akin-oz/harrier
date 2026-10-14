import { useQuery, useQueryClient } from "@tanstack/react-query";

import { TelegramForm } from "../../features/notifications/TelegramForm";
import type { TelegramStatus } from "../../features/notifications/TelegramForm";
import { api } from "../../shared/api/client";
import { refusalMessage } from "../../shared/api/refusal";

const KEY = ["settings", "telegram"] as const;

async function fetchTelegram(): Promise<TelegramStatus> {
  const { data, error } = await api.GET("/notifications/telegram");
  if (error !== undefined) {
    throw new Error(refusalMessage(error, "could not read the Telegram settings"));
  }
  return data;
}

/**
 * The owner's Telegram settings (spec 114, amendment of 2026-10-11). Hosted
 * only: locally Telegram is `.env`, and the route answers 404.
 */
export function TelegramSection() {
  const queryClient = useQueryClient();
  const telegram = useQuery({ queryKey: KEY, queryFn: fetchTelegram });

  if (telegram.isPending) return <p className="settings-muted">Reading the settings…</p>;
  if (telegram.isError) {
    return (
      <p role="alert" className="settings-error">
        {telegram.error.message}
      </p>
    );
  }
  return (
    <TelegramForm
      status={telegram.data}
      onChanged={(next) => {
        queryClient.setQueryData<TelegramStatus>(KEY, next);
      }}
    />
  );
}
