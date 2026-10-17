import { useMutation } from "@tanstack/react-query";

import { useRunStream } from "../../features/runs/useRunStream";
import type { RunOut } from "../../features/runs/useRunStream";
import { api } from "../../shared/api/client";
import { RunOutput, refusalMessage } from "./RunOutput";

/**
 * The learned score (spec 077) as two runs: the export
 * (`harrier scoring export`), whose log ends with its counts and the reasons
 * rows were left out, and training (`harrier scoring train --activate`,
 * spec 157), which reads the newest export and activates the model only if
 * it beats the rules. Each streams its own output.
 */
export function ScoringSection() {
  const exportStream = useRunStream();
  const exportStart = useMutation({
    mutationFn: async (): Promise<RunOut> => {
      const { data, error } = await api.POST("/ops/scoring/export");
      if (error !== undefined) throw new Error(refusalMessage(error, "the export was refused"));
      return data;
    },
    onSuccess: (data) => {
      exportStream.begin(data);
    },
  });

  const trainStream = useRunStream();
  const trainStart = useMutation({
    mutationFn: async (): Promise<RunOut> => {
      const { data, error } = await api.POST("/ops/scoring/train", {
        body: { activate: true },
      });
      if (error !== undefined) throw new Error(refusalMessage(error, "training was refused"));
      return data;
    },
    onSuccess: (data) => {
      trainStream.begin(data);
    },
  });

  return (
    <section className="ops-section" aria-labelledby="ops-scoring">
      <h3 id="ops-scoring" className="ops-section__heading">
        Learned score
      </h3>
      <p className="ops-note">
        Writes the labelled feature export the trainer reads. Training uses the newest export, and
        activates the model only if it beats the rules.
      </p>
      <div className="ops-actions">
        <button
          type="button"
          disabled={exportStream.active || exportStart.isPending}
          onClick={() => {
            exportStart.mutate();
          }}
        >
          Write the feature export
        </button>
        <button
          type="button"
          disabled={trainStream.active || trainStart.isPending}
          onClick={() => {
            trainStart.mutate();
          }}
        >
          Train the model
        </button>
      </div>
      {exportStart.error !== null && (
        <p role="alert" className="ops-error">
          {exportStart.error.message}
        </p>
      )}
      {trainStart.error !== null && (
        <p role="alert" className="ops-error">
          {trainStart.error.message}
        </p>
      )}
      <RunOutput stream={exportStream} label="Feature export" />
      <RunOutput stream={trainStream} label="Model training" />
    </section>
  );
}
