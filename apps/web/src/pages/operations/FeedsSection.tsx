import { useMutation } from "@tanstack/react-query";
import { useState } from "react";

import { useRunStream } from "../../features/runs/useRunStream";
import type { EventSourceFactory, RunOut } from "../../features/runs/useRunStream";
import { api } from "../../shared/api/client";
import { RunOutput, TOKEN_REFUSED, refusalMessage } from "./RunOutput";

/**
 * Feed health (spec 025). Checking probes every configured board and
 * changes nothing. Pruning is a separate action with its own confirmation,
 * never a checkbox on the check, because it removes boards from the stored
 * watchlist; its log names each removed URL.
 */
export function FeedsSection({ createEventSource }: { createEventSource: EventSourceFactory }) {
  const stream = useRunStream(createEventSource);
  const [confirming, setConfirming] = useState(false);

  const check = useMutation({
    mutationFn: async (): Promise<RunOut> => {
      const { data } = await api.POST("/ops/feeds");
      if (data === undefined) throw new Error(TOKEN_REFUSED);
      return data;
    },
    onSuccess: (data) => {
      stream.begin(data);
    },
  });

  const prune = useMutation({
    mutationFn: async (): Promise<RunOut> => {
      const { data, error } = await api.POST("/ops/feeds/prune", { body: { confirm: true } });
      if (error !== undefined) throw new Error(refusalMessage(error, "the prune was refused"));
      if (data === undefined) throw new Error(TOKEN_REFUSED);
      return data;
    },
    onSuccess: (data) => {
      setConfirming(false);
      stream.begin(data);
    },
  });

  const busy = stream.active || check.isPending || prune.isPending;
  const failure = check.error ?? prune.error;

  return (
    <section className="ops-section" aria-labelledby="ops-feeds">
      <h3 id="ops-feeds" className="ops-section__heading">
        Feed health
      </h3>
      <p className="ops-note">
        Probes each configured board once. A board that answers 404 or 410 is dead; anything else
        that fails is unreachable, and an unreachable board is never pruned.
      </p>
      <div className="ops-actions">
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            check.mutate();
          }}
        >
          Check feeds
        </button>
        <button
          type="button"
          className="ops-button--caution"
          disabled={busy}
          aria-expanded={confirming}
          onClick={() => {
            setConfirming(true);
          }}
        >
          Prune dead boards…
        </button>
      </div>
      {confirming && (
        <div className="ops-confirm" role="group" aria-label="Confirm pruning">
          <p>
            This checks every board again and removes the ones that answer as dead from the stored
            watchlist. The log lists each removed URL.
          </p>
          <div className="ops-actions">
            <button
              type="button"
              className="ops-button--caution"
              disabled={busy}
              onClick={() => {
                prune.mutate();
              }}
            >
              Prune the dead boards
            </button>
            <button
              type="button"
              onClick={() => {
                setConfirming(false);
              }}
            >
              Keep them
            </button>
          </div>
        </div>
      )}
      {failure !== null && (
        <p role="alert" className="ops-error">
          {failure.message}
        </p>
      )}
      <RunOutput stream={stream} label="Feed check" />
    </section>
  );
}
