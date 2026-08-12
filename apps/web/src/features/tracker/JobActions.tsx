import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import type { Job } from "../../entities/job";
import { api } from "../../shared/api/client";
import "./JobActions.css";

// The same five verbs the CLI has, named the same way. The mapping lives in
// `harrier.tracker.actions.STATUS_BY_VERB` and a test asserts this set cannot
// drift from it, so the browser cannot offer a transition the command line
// lacks (spec 042).
const VERBS = [
  { verb: "shortlist", label: "Shortlist", from: ["prospect"] },
  { verb: "track", label: "Request CV", from: ["prospect", "shortlisted"] },
  { verb: "applied", label: "Applied", from: ["prospect", "shortlisted", "tailored_cv_requested"] },
  { verb: "interviewing", label: "Interviewing", from: ["applied"] },
  { verb: "reject", label: "Reject", from: [] },
] as const;

type Props = { job: Job };

export function JobActions({ job }: Props) {
  const queryClient = useQueryClient();
  const [reason, setReason] = useState("");
  const [asking, setAsking] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const change = useMutation({
    mutationFn: async ({ verb, why }: { verb: string; why?: string }) => {
      const { data, error } = await api.POST("/tracker/{selector}/status", {
        params: { path: { selector: String(job.id) } },
        body: { verb, reason: why ?? null },
      });
      // A refusal is a normal outcome here, not an exception to swallow: the
      // tracker declines transitions and the operator needs the reason it
      // gave, in the words it gave them.
      if (error !== undefined) {
        throw new Error(refusalMessage(error));
      }
      if (data === undefined) {
        throw new Error("the local API token was not accepted");
      }
      return data;
    },
    onSuccess: () => {
      setFailure(null);
      setAsking(false);
      setReason("");
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError: (error: Error) => {
      setFailure(error.message);
    },
  });

  const rescore = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/tracker/{selector}/rescore", {
        params: { path: { selector: String(job.id) } },
      });
      if (error !== undefined) throw new Error(refusalMessage(error));
      if (data === undefined) throw new Error("the local API token was not accepted");
      return data;
    },
    onSuccess: (result) => {
      setFailure(null);
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
      if (result.previous !== String(result.current)) {
        setFailure(`rescored ${result.previous || "-"} to ${String(result.current)}`);
      }
    },
    onError: (error: Error) => {
      setFailure(error.message);
    },
  });

  const busy = change.isPending || rescore.isPending;

  return (
    <div className="job-actions">
      {/* While a rejection reason is being typed, the row is mid-decision and
          the other verbs are not what to do next. They used to stay live, so
          a click landed a status change on a row the operator was in the
          middle of rejecting (review finding on PR #41). */}
      {!asking &&
        VERBS.filter((entry) => entry.verb !== "reject").map((entry) => (
          <button
            key={entry.verb}
            type="button"
            disabled={busy || job.status === "rejected"}
            onClick={() => {
              change.mutate({ verb: entry.verb });
            }}
          >
            {entry.label}
          </button>
        ))}

      {!asking && (
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            rescore.mutate();
          }}
        >
          Rescore
        </button>
      )}

      {asking ? (
        <span className="job-actions__reason">
          <input
            aria-label="Rejection reason"
            value={reason}
            placeholder="why?"
            onChange={(event) => {
              setReason(event.target.value);
            }}
          />
          <button
            type="button"
            disabled={busy || reason.trim() === ""}
            onClick={() => {
              change.mutate({ verb: "reject", why: reason.trim() });
            }}
          >
            Confirm
          </button>
          <button
            type="button"
            onClick={() => {
              setAsking(false);
            }}
          >
            Cancel
          </button>
        </span>
      ) : (
        <button
          type="button"
          className="job-actions__reject"
          disabled={busy || job.status === "rejected"}
          onClick={() => {
            setAsking(true);
          }}
        >
          Reject
        </button>
      )}

      {failure !== null && (
        <p className="job-actions__failure" role="status">
          {failure}
        </p>
      )}
    </div>
  );
}

// The API answers a refusal with a `detail` string that is the message the
// domain wrote. Showing it verbatim is the point: the CLI prints the same
// words, and a UI that paraphrased them would be a second implementation of
// the explanation.
function refusalMessage(error: unknown): string {
  if (typeof error === "object" && error !== null && "detail" in error) {
    const detail = (error as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
  }
  return "the tracker refused that change";
}
