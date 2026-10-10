import { useMutation } from "@tanstack/react-query";
import { useState } from "react";

import { useRunStream } from "../../features/runs/useRunStream";
import type { EventSourceFactory, RunOut } from "../../features/runs/useRunStream";
import { api } from "../../shared/api/client";
import { RunOutput, TOKEN_REFUSED, refusalMessage } from "./RunOutput";
import { SOURCE_LABEL, isSource } from "./sources";
import type { SourceName } from "./sources";

/**
 * Reconsideration (spec 031). The first request reports what would be
 * cleared and clears nothing. Clearing is a second, separate request,
 * offered only after a report has finished, so the operator has read the
 * count before anything changes.
 */
export function ReconsiderSection({
  createEventSource,
}: {
  createEventSource: EventSourceFactory;
}) {
  const stream = useRunStream(createEventSource);
  const [source, setSource] = useState<SourceName | null>(null);
  // What the last run did and for which source, so "clear" acts on the
  // report the operator read rather than on a selection changed since.
  const [last, setLast] = useState<{ apply: boolean; source: SourceName | null } | null>(null);

  const start = useMutation({
    mutationFn: async (request: { apply: boolean; source: SourceName | null }): Promise<RunOut> => {
      const { data, error } = await api.POST("/ops/reconsider", { body: request });
      if (error !== undefined) {
        throw new Error(refusalMessage(error, "reconsideration was refused"));
      }
      if (data === undefined) throw new Error(TOKEN_REFUSED);
      return data;
    },
    onSuccess: (data, request) => {
      setLast(request);
      stream.begin(data);
    },
  });

  const busy = stream.active || start.isPending;
  const reported =
    last !== null && !last.apply && stream.run !== null && stream.run.state === "succeeded";

  return (
    <section className="ops-section" aria-labelledby="ops-reconsider">
      <h3 id="ops-reconsider" className="ops-section__heading">
        Reconsideration
      </h3>
      <p className="ops-note">
        Re-opens rejections made under screening rules that have since changed. A posting you
        rejected yourself is never re-opened.
      </p>
      <div className="ops-actions">
        <label className="ops-field">
          <span>Source</span>
          <select
            value={source ?? ""}
            onChange={(event) => {
              setSource(isSource(event.target.value) ? event.target.value : null);
            }}
          >
            <option value="">Every source</option>
            {Object.entries(SOURCE_LABEL).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            start.mutate({ apply: false, source });
          }}
        >
          Report what would be cleared
        </button>
        {reported && (
          <button
            type="button"
            className="ops-button--caution"
            disabled={busy}
            onClick={() => {
              start.mutate({ apply: true, source: last.source });
            }}
          >
            Clear what the report found
          </button>
        )}
      </div>
      {start.error !== null && (
        <p role="alert" className="ops-error">
          {start.error.message}
        </p>
      )}
      <RunOutput
        stream={stream}
        label={last?.apply === true ? "Reconsideration" : "Reconsideration report"}
      />
    </section>
  );
}
