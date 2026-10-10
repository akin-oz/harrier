import { useMutation } from "@tanstack/react-query";

import { useRunStream } from "../../features/runs/useRunStream";
import type { EventSourceFactory, RunOut } from "../../features/runs/useRunStream";
import { api } from "../../shared/api/client";
import { RunOutput, TOKEN_REFUSED } from "./RunOutput";

/**
 * The learned score's export (`harrier scoring export`, spec 077), as a run
 * whose log ends with its counts and the reasons rows were left out.
 * Training needs scikit-learn, which the image does not install, so it runs
 * on the host.
 */
export function ScoringSection({ createEventSource }: { createEventSource: EventSourceFactory }) {
  const stream = useRunStream(createEventSource);
  const start = useMutation({
    mutationFn: async (): Promise<RunOut> => {
      const { data } = await api.POST("/ops/scoring/export");
      if (data === undefined) throw new Error(TOKEN_REFUSED);
      return data;
    },
    onSuccess: (data) => {
      stream.begin(data);
    },
  });

  return (
    <section className="ops-section" aria-labelledby="ops-scoring">
      <h3 id="ops-scoring" className="ops-section__heading">
        Learned score
      </h3>
      <p className="ops-note">
        Writes the labelled feature export the trainer reads. Training runs on the host, where
        scikit-learn is installed; this server does not have it.
      </p>
      <div className="ops-actions">
        <button
          type="button"
          disabled={stream.active || start.isPending}
          onClick={() => {
            start.mutate();
          }}
        >
          Write the feature export
        </button>
      </div>
      {start.error !== null && (
        <p role="alert" className="ops-error">
          {start.error.message}
        </p>
      )}
      <RunOutput stream={stream} label="Feature export" />
    </section>
  );
}
