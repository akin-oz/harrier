import { useMutation } from "@tanstack/react-query";
import { useState } from "react";

import type { components } from "@harrier/contract";

import { useRunStream } from "../../features/runs/useRunStream";
import type { EventSourceFactory, RunOut } from "../../features/runs/useRunStream";
import { api } from "../../shared/api/client";
import { RunOutput, TOKEN_REFUSED, countIn, refusalMessage } from "./RunOutput";

type Options = components["schemas"]["EvaluateProspectsIn"];

/**
 * Batch evaluation of prospects (`harrier evaluate-prospects`, spec 015). A
 * run reports verdicts and rejects nothing. Rejecting is a second run with
 * `apply`, offered only after a report, naming how many it would reject;
 * each rejection is recorded as the system's decision (spec 079), which the
 * operator can reopen.
 */
export function EvaluationSection({
  createEventSource,
}: {
  createEventSource: EventSourceFactory;
}) {
  const stream = useRunStream(createEventSource);
  const [threshold, setThreshold] = useState("");
  const [limit, setLimit] = useState("");
  const [refresh, setRefresh] = useState(false);
  const [borderline, setBorderline] = useState(false);
  // The options of the last report, so "reject" applies what was read.
  const [reported, setReported] = useState<Options | null>(null);
  const [applied, setApplied] = useState(false);

  const start = useMutation({
    mutationFn: async (body: Options): Promise<RunOut> => {
      const { data, error } = await api.POST("/ops/evaluate-prospects", { body });
      if (error !== undefined) throw new Error(refusalMessage(error, "evaluation was refused"));
      if (data === undefined) throw new Error(TOKEN_REFUSED);
      return data;
    },
    onSuccess: (data, body) => {
      setApplied(body.apply === true);
      setReported(body.apply === true ? null : body);
      stream.begin(data);
    },
  });

  function options(): Options {
    // An empty field is left out, so the CLI's own default applies.
    return {
      apply: false,
      threshold: threshold.trim() === "" ? null : Number(threshold),
      limit: limit.trim() === "" ? null : Number(limit),
      refresh,
      include_borderline: borderline,
    };
  }

  const busy = stream.active || start.isPending;
  const wouldReject =
    reported !== null && !applied && stream.run?.state === "succeeded"
      ? countIn(stream.lines, /^would_reject=(\d+)$/)
      : null;

  return (
    <section className="ops-section" aria-labelledby="ops-evaluate">
      <h3 id="ops-evaluate" className="ops-section__heading">
        Batch evaluation
      </h3>
      <p className="ops-note">
        Evaluates every prospect with one model call each. Leave a field empty for the command
        line&apos;s default.
      </p>
      <div className="ops-actions">
        <label className="ops-field">
          <span>Confidence to reject, 0 to 1</span>
          <input
            inputMode="decimal"
            value={threshold}
            onChange={(event) => {
              setThreshold(event.target.value);
            }}
          />
        </label>
        <label className="ops-field">
          <span>Only the first</span>
          <input
            inputMode="numeric"
            value={limit}
            onChange={(event) => {
              setLimit(event.target.value);
            }}
          />
        </label>
        <label className="ops-check">
          <input
            type="checkbox"
            checked={refresh}
            onChange={(event) => {
              setRefresh(event.target.checked);
            }}
          />
          <span>Re-evaluate prospects already evaluated</span>
        </label>
        <label className="ops-check">
          <input
            type="checkbox"
            checked={borderline}
            onChange={(event) => {
              setBorderline(event.target.checked);
            }}
          />
          <span>Also reject borderline verdicts</span>
        </label>
      </div>
      <div className="ops-actions">
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            start.mutate(options());
          }}
        >
          Evaluate, reject nothing
        </button>
        {wouldReject !== null && wouldReject > 0 && reported !== null && (
          <button
            type="button"
            className="ops-button--caution"
            disabled={busy}
            onClick={() => {
              start.mutate({ ...reported, apply: true });
            }}
          >
            Reject the {wouldReject} prospects as the system&apos;s decision
          </button>
        )}
      </div>
      {start.error !== null && (
        <p role="alert" className="ops-error">
          {start.error.message}
        </p>
      )}
      <RunOutput stream={stream} label={applied ? "Rejection" : "Evaluation"} />
    </section>
  );
}
