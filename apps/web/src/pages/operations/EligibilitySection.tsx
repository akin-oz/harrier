import type { components } from "@harrier/contract";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { VERDICT_LABEL, VERDICT_ORDER } from "../../entities/job";
import { api } from "../../shared/api/client";
import { trackKey } from "../../shared/track";
import { refusalMessage } from "./RunOutput";

type Report = components["schemas"]["EligibilityReassessOut"];

/**
 * Türkiye eligibility (spec 123), in Reconsideration's shape. The first
 * request reports what the rules would assess and writes nothing. Writing is
 * a second, separate request, offered only after a report has come back. The
 * rows may change between the two, so the written report is the second
 * request's own, and is labelled as such.
 *
 * The route answers at once with counts, so this section waits on the
 * request itself and does not stream a run.
 */
export function EligibilitySection() {
  const queryClient = useQueryClient();
  const [shown, setShown] = useState<Report | null>(null);

  const run = useMutation({
    mutationFn: async (apply: boolean): Promise<Report> => {
      const { data, error } = await api.POST("/ops/eligibility-reassess", { body: { apply } });
      if (error !== undefined) throw new Error(refusalMessage(error, "reassessment was refused"));
      return data;
    },
    onSuccess: (report) => {
      setShown(report);
      // A write changes what the default track's rows read; reassess runs
      // on the default track only (spec 122).
      if (report.applied) void queryClient.invalidateQueries({ queryKey: trackKey(null, "jobs") });
    },
  });

  const reported = shown !== null && !shown.applied;

  return (
    <section className="ops-section" aria-labelledby="ops-eligibility">
      <h3 id="ops-eligibility" className="ops-section__heading">
        Türkiye eligibility
      </h3>
      <p className="ops-note">
        Runs the eligibility rules again on open rows with a stored description. Your own verdicts
        stay current, and a rejected row is never assessed.
      </p>
      <div className="ops-actions">
        <button
          type="button"
          disabled={run.isPending}
          onClick={() => {
            run.mutate(false);
          }}
        >
          Report what would be assessed
        </button>
        {reported && (
          <button
            type="button"
            className="ops-button--caution"
            disabled={run.isPending}
            onClick={() => {
              run.mutate(true);
            }}
          >
            Assess what the report found
          </button>
        )}
      </div>
      {run.isPending && <p className="ops-muted">Working…</p>}
      {run.error !== null && (
        <p role="alert" className="ops-error">
          {run.error.message}
        </p>
      )}
      {/* The region is here before the first run, so the report is announced
          when it arrives (spec 156). */}
      <div role="status" aria-label="Eligibility report">
        {shown !== null && <ReportView report={shown} />}
      </div>
    </section>
  );
}

function ReportView({ report }: { report: Report }) {
  // `by_verdict` is a free string map in the contract. The list is the four
  // verdicts in their order, so a key outside them is never shown (spec 123).
  const counts = new Map(Object.entries(report.by_verdict));
  return (
    <div className="ops-outcome">
      <p>
        {report.applied ? "Written. Counts are from this run. " : "Nothing written yet. "}
        {report.applied ? "Assessed" : "Would assess"} {report.assessed} of {report.examined} rows:{" "}
        {report.unchanged} unchanged, {report.rejected_skipped} rejected and skipped,{" "}
        {report.no_description} with no stored description.
      </p>
      <ul>
        {VERDICT_ORDER.map((verdict) => (
          <li key={verdict}>
            {VERDICT_LABEL[verdict]}: {counts.get(verdict) ?? 0}
          </li>
        ))}
      </ul>
      {report.under_candidate > 0 && (
        <p>
          {report.under_candidate} have your verdict, which stays current; the rules now differ on{" "}
          {report.disagrees}.
        </p>
      )}
    </div>
  );
}
