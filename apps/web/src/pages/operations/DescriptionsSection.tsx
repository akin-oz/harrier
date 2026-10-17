import { useMutation } from "@tanstack/react-query";
import { useState } from "react";

import { useRunStream } from "../../features/runs/useRunStream";
import type { RunOut } from "../../features/runs/useRunStream";
import { api } from "../../shared/api/client";
import { RunOutput, refusalMessage } from "./RunOutput";

/**
 * Missing job descriptions (`harrier description backfill`, spec 151). A dry
 * run first, which lists what it would fetch and requests nothing; fetching
 * is a separate action. Each description comes from its posting's page.
 */
export function DescriptionsSection() {
  const stream = useRunStream();
  const [fetching, setFetching] = useState<boolean | null>(null);

  const start = useMutation({
    mutationFn: async (dryRun: boolean): Promise<RunOut> => {
      const { data, error } = await api.POST("/ops/descriptions/backfill", {
        body: { dry_run: dryRun },
      });
      if (error !== undefined) throw new Error(refusalMessage(error, "the backfill was refused"));
      return data;
    },
    onSuccess: (data, dryRun) => {
      setFetching(!dryRun);
      stream.begin(data);
    },
  });

  const busy = stream.active || start.isPending;

  return (
    <section className="ops-section" aria-labelledby="ops-descriptions">
      <h3 id="ops-descriptions" className="ops-section__heading">
        Job descriptions
      </h3>
      <p className="ops-note">
        Fetches each missing job description from its posting page: LinkedIn&apos;s public page or
        an allow-listed ATS. A closed posting cannot be fetched; paste those on the Apply page.
      </p>
      <div className="ops-actions">
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            start.mutate(true);
          }}
        >
          Dry run
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            start.mutate(false);
          }}
        >
          Fetch missing
        </button>
      </div>
      {start.error !== null && (
        <p role="alert" className="ops-error">
          {start.error.message}
        </p>
      )}
      <RunOutput
        stream={stream}
        label={fetching === true ? "Description fetch" : "Description dry run"}
      />
    </section>
  );
}
