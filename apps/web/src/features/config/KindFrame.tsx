import { useEffect, useId, useRef, useState } from "react";
import type { KeyboardEvent, ReactNode, SyntheticEvent } from "react";

import type { ConfigEntry } from "./useKindEditor";
import "./config.css";

interface Pending {
  isPending: boolean;
  error: Error | null;
  mutate: () => void;
}

/**
 * What every configuration editor shares: where the value comes from, the
 * save, the refusal in the store's words, and "Reset to the file" behind a
 * confirmation that says what it does (spec 096).
 */
export function KindFrame({
  entry,
  title,
  description,
  notice,
  save,
  reset,
  blocked,
  children,
}: {
  entry: ConfigEntry;
  title: string;
  description: ReactNode;
  notice: string | null;
  save: Pending;
  reset: Pending;
  // A reason the browser will not send the draft, said beside the button.
  blocked?: string | null;
  children: ReactNode;
}) {
  const headingId = useId();
  const [asking, setAsking] = useState(false);
  const cancelRef = useRef<HTMLButtonElement | null>(null);
  const openerRef = useRef<HTMLButtonElement | null>(null);
  const busy = save.isPending || reset.isPending;

  // Focus goes to Cancel, the safe answer, when the question opens, and back
  // to the button that opened it when it closes. The opener is not rendered
  // while the question shows, so it is focused after it mounts again.
  const wasAsking = useRef(false);
  useEffect(() => {
    if (asking) cancelRef.current?.focus();
    else if (wasAsking.current) openerRef.current?.focus();
    wasAsking.current = asking;
  }, [asking]);

  function cancel(): void {
    setAsking(false);
  }

  function onKey(event: KeyboardEvent<HTMLDivElement>): void {
    if (event.key !== "Escape") return;
    event.preventDefault();
    cancel();
  }

  function submit(event: SyntheticEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (blocked) return;
    save.mutate();
  }

  const refusal = save.error ?? reset.error;

  return (
    <section className="config-kind" aria-labelledby={headingId}>
      <div className="config-kind__head">
        <h4 id={headingId} className="config-kind__title">
          {title}
        </h4>
        <Source entry={entry} />
      </div>
      <p className="config-kind__description">{description}</p>
      {entry.error !== null && (
        <p role="alert" className="config-kind__refusal">
          The stored value cannot be read: {entry.error}
        </p>
      )}
      <form className="config-kind__form" onSubmit={submit}>
        {children}
        <div className="config-kind__actions">
          <button
            type="submit"
            className="config-button config-button--primary"
            disabled={busy}
            aria-describedby={blocked ? `${headingId}-blocked` : undefined}
          >
            Save {title.toLowerCase()}
          </button>
          {blocked && (
            <span id={`${headingId}-blocked`} className="config-kind__blocked">
              {blocked}
            </span>
          )}
          {!asking && (
            <button
              ref={openerRef}
              type="button"
              className="config-button"
              disabled={busy}
              onClick={() => {
                setAsking(true);
              }}
            >
              Reset to the file
            </button>
          )}
        </div>
      </form>
      {asking && (
        <div
          className="config-kind__confirm"
          role="group"
          aria-label={`Reset ${title.toLowerCase()} to the file`}
          onKeyDown={onKey}
        >
          <p className="config-kind__question">
            Remove the stored {title.toLowerCase()}? The stored value is deleted and the file in the
            checkout is used instead.
          </p>
          <div className="config-kind__actions">
            <button
              type="button"
              className="config-button config-button--danger"
              disabled={busy}
              onClick={() => {
                setAsking(false);
                reset.mutate();
              }}
            >
              Remove and use the file
            </button>
            <button ref={cancelRef} type="button" className="config-button" onClick={cancel}>
              Cancel
            </button>
          </div>
        </div>
      )}
      {refusal !== null && (
        <p role="alert" className="config-kind__refusal">
          {refusal.message}
        </p>
      )}
      <p role="status" className="config-kind__notice">
        {notice}
      </p>
    </section>
  );
}

function Source({ entry }: { entry: ConfigEntry }) {
  if (entry.source === "store") {
    const when = (entry.updated_at ?? "").slice(0, 16);
    return (
      <span className="config-source config-source--store">
        From the store{when ? `, saved ${when}` : ""}
      </span>
    );
  }
  return <span className="config-source">From the file in the checkout</span>;
}
