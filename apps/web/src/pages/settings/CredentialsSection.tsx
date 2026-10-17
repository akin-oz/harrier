import { useQuery, useQueryClient } from "@tanstack/react-query";

import type { components } from "@harrier/contract";

import { CredentialForm, stateOf } from "../../features/credentials/CredentialForm";
import type { CredentialStatus, FormKind } from "../../features/credentials/CredentialForm";
import { api } from "../../shared/api/client";
import { refusalMessage } from "../../shared/api/refusal";

type Credentials = components["schemas"]["CredentialsOut"];

export const CREDENTIALS_KEY = ["settings", "credentials"] as const;
const KEY = CREDENTIALS_KEY;

export async function fetchCredentials(): Promise<Credentials> {
  const { data, error } = await api.GET("/credentials");
  if (error !== undefined) {
    throw new Error(refusalMessage(error, "could not read the credentials"));
  }
  return data;
}

/**
 * Each credential kind's state, from `GET /credentials`, with a form for the
 * three kinds that take a value here (spec 114, amendment of 2026-10-11).
 * Hosted only: locally credentials are `.env`, and the route answers 404.
 */
export function CredentialsSection() {
  const queryClient = useQueryClient();
  const credentials = useQuery({ queryKey: KEY, queryFn: fetchCredentials });

  function changed(next: CredentialStatus): void {
    queryClient.setQueryData<Credentials>(KEY, (existing) =>
      existing === undefined
        ? existing
        : {
            credentials: existing.credentials.map((item) =>
              item.kind === next.kind ? next : item,
            ),
          },
    );
  }

  if (credentials.isPending) return <p className="settings-muted">Reading the credentials…</p>;
  if (credentials.isError) {
    return (
      <p role="alert" className="settings-error">
        {credentials.error.message}
      </p>
    );
  }
  return (
    <div className="settings-credentials">
      <p className="settings-muted">
        Saved encrypted and never shown again. Each form replaces the stored value.
      </p>
      {credentials.data.credentials.map((status) =>
        status.kind === "telegram" ? (
          <p key={status.kind} className="settings-muted">
            Telegram: {stateOf(status)}. It has its own form, below.
          </p>
        ) : (
          <CredentialForm
            key={status.kind}
            kind={status.kind satisfies FormKind}
            status={status}
            onChanged={changed}
          />
        ),
      )}
    </div>
  );
}
