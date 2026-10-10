import { useMutation } from "@tanstack/react-query";
import { useState } from "react";

import { useRunStream } from "../../features/runs/useRunStream";
import type { EventSourceFactory, RunOut } from "../../features/runs/useRunStream";
import { api } from "../../shared/api/client";
import { RunOutput, TOKEN_REFUSED, countIn } from "./RunOutput";

/**
 * Decision history backfill (`harrier events backfill`, spec 079). A dry
 * run first, which prints the counts and writes nothing; writing is a
 * separate action that names the count the dry run printed.
 */
export function HistorySection({ createEventSource }: { createEventSource: EventSourceFactory }) {
  const stream = useRunStream(createEventSource);
  const [wrote, setWrote] = useState<boolean | null>(null);

  const start = useMutation({
    mutationFn: async (dryRun: boolean): Promise<RunOut> => {
      const { data } = await api.POST("/ops/events/backfill", { body: { dry_run: dryRun } });
      if (data === undefined) throw new Error(TOKEN_REFUSED);
      return data;
    },
    onSuccess: (data, dryRun) => {
      setWrote(!dryRun);
      stream.begin(data);
    },
  });

  const busy = stream.active || start.isPending;
  const counted =
    wrote === false && stream.run?.state === "succeeded"
      ? countIn(stream.lines, /would write (\d+) events/)
      : null;

  return (
    <section className="ops-section" aria-labelledby="ops-history">
      <h3 id="ops-history" className="ops-section__heading">
        Decision history
      </h3>
      <p className="ops-note">
        Reconstructs events for rows decided before history was recorded. Each one is marked as
        reconstructed, so its time is not read as the time of the decision.
      </p>
      <div className="ops-actions">
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            start.mutate(true);
          }}
        >
          Count what a backfill would write
        </button>
        {counted !== null && counted > 0 && (
          <button
            type="button"
            className="ops-button--caution"
            disabled={busy}
            onClick={() => {
              start.mutate(false);
            }}
          >
            Write the {counted} reconstructed events
          </button>
        )}
      </div>
      {start.error !== null && (
        <p role="alert" className="ops-error">
          {start.error.message}
        </p>
      )}
      <RunOutput stream={stream} label={wrote === true ? "Backfill" : "Backfill count"} />
    </section>
  );
}
