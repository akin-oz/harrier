import { useId, useState } from "react";
import type { SyntheticEvent } from "react";

import type { components } from "@harrier/contract";

import { api } from "../../shared/api/client";
import { refusalMessage } from "../../shared/api/refusal";
import "./credentials.css";

export type CredentialStatus = components["schemas"]["CredentialStatusOut"];
type AiProvider = components["schemas"]["AiCredentialIn"]["provider"];

/** The kinds this form takes. `telegram` has its own form and route (spec 109). */
export type FormKind = Exclude<CredentialStatus["kind"], "telegram">;

const LABEL: Record<FormKind, string> = {
  ai: "AI provider key",
  apify: "Apify token",
  hunter: "Hunter key",
};

const PROVIDERS: readonly AiProvider[] = ["openai-api", "anthropic-api"];

/** Words for a kind's state, never a value: the API returns none. */
export function stateOf(status: CredentialStatus): string {
  return status.set ? `Set, updated ${status.updated_at ?? "at an unknown time"}` : "Not set";
}

/**
 * One credential kind (spec 114, amendment of 2026-10-11): its state, a form
 * to replace it, and a way to remove it.
 *
 * Every secret field is a password input with autocomplete off. A submitted
 * value is held in this component's state and goes only to the save request:
 * never rendered back, put in a URL, stored by this code or logged. The
 * fields clear when the save is accepted, and the page offers no reveal,
 * because the API keeps no value to return (spec 116).
 */
export function CredentialForm({
  kind,
  status,
  onChanged,
}: {
  kind: FormKind;
  status: CredentialStatus;
  onChanged: (next: CredentialStatus) => void;
}) {
  const id = useId();
  const [provider, setProvider] = useState<AiProvider>("openai-api");
  const [secret, setSecret] = useState("");
  const [model, setModel] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);

  async function save(event: SyntheticEvent): Promise<void> {
    event.preventDefault();
    setBusy(true);
    setProblem(null);
    const result =
      kind === "ai"
        ? await api.PUT("/credentials/ai", {
            body: { provider, key: secret, ...(model.trim() === "" ? {} : { model }) },
          })
        : kind === "apify"
          ? await api.PUT("/credentials/apify", { body: { token: secret } })
          : await api.PUT("/credentials/hunter", { body: { key: secret } });
    setBusy(false);
    if (result.error !== undefined) {
      setProblem(refusalMessage(result.error, "the credential was refused"));
      return;
    }
    setSecret("");
    setModel("");
    setProvider("openai-api");
    onChanged(result.data);
  }

  async function remove(): Promise<void> {
    setBusy(true);
    setProblem(null);
    const { data, error } = await api.DELETE("/credentials/{kind}", { params: { path: { kind } } });
    setBusy(false);
    setConfirming(false);
    if (error !== undefined) {
      setProblem(refusalMessage(error, "the credential was not removed"));
      return;
    }
    onChanged(data);
  }

  return (
    <form
      className="credential-form"
      aria-label={LABEL[kind]}
      onSubmit={(event) => {
        void save(event);
      }}
    >
      <h4 className="credential-form__title">{LABEL[kind]}</h4>
      <p className="credential-form__state">{stateOf(status)}</p>
      {kind === "ai" && (
        <div className="credential-form__field">
          <label htmlFor={`${id}-provider`}>Provider</label>
          <select
            id={`${id}-provider`}
            value={provider}
            onChange={(event) => {
              setProvider(event.target.value as AiProvider);
            }}
          >
            {PROVIDERS.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </div>
      )}
      <div className="credential-form__field">
        <label htmlFor={`${id}-secret`}>{kind === "apify" ? "Token" : "Key"}</label>
        <input
          id={`${id}-secret`}
          type="password"
          autoComplete="off"
          required
          value={secret}
          onChange={(event) => {
            setSecret(event.target.value);
          }}
        />
      </div>
      {kind === "ai" && (
        <div className="credential-form__field">
          <label htmlFor={`${id}-model`}>Model (optional)</label>
          <input
            id={`${id}-model`}
            type="text"
            autoComplete="off"
            value={model}
            onChange={(event) => {
              setModel(event.target.value);
            }}
          />
        </div>
      )}
      <div className="credential-form__actions">
        <button type="submit" disabled={busy}>
          Save {LABEL[kind].toLowerCase()}
        </button>
        {status.set && !confirming && (
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              setConfirming(true);
            }}
          >
            Remove
          </button>
        )}
        {confirming && (
          <span role="group" aria-label={`Confirm removing the ${LABEL[kind].toLowerCase()}`}>
            Remove it?{" "}
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                void remove();
              }}
            >
              Yes, remove
            </button>{" "}
            <button
              type="button"
              onClick={() => {
                setConfirming(false);
              }}
            >
              Keep it
            </button>
          </span>
        )}
      </div>
      {problem !== null && (
        <p role="alert" className="credential-form__problem">
          {problem}
        </p>
      )}
    </form>
  );
}
