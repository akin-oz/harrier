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

// The one verb that is what to do next from this status. The rest stay
// reachable behind a disclosure, which is what keeps the actions column
// narrow enough that the table does not scroll sideways. Every verb is still
// on the row; none is removed.
function forwardVerb(status: string): (typeof VERBS)[number] | null {
  return VERBS.find((entry) => (entry.from as readonly string[]).includes(status)) ?? null;
}

// `onApply` is passed in rather than the page rendering its own button
// beside this one: two separate controls stacked into two rows and made the
// table row twice as tall as it needed to be. What to do next for this job
// belongs on one line.
type Props = { job: Job; onApply?: (job: Job) => void };

// The operator's four most frequent rejection reasons, submitted verbatim so
// the stored values stay groupable. Shortcuts, not an enum: the API keeps
// accepting any string and `other…` still reaches the free-text input
// (spec 056).
const FREQUENT_REASONS = ["hybrid", "onsite", "closed", "missing stack"] as const;

export function JobActions({ job, onApply }: Props) {
  const queryClient = useQueryClient();
  const [reason, setReason] = useState("");
  const [asking, setAsking] = useState(false);
  const [otherOpen, setOtherOpen] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
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
      setOtherOpen(false);
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
  const primary = forwardVerb(job.status);
  const closed = job.status === "rejected";
  // Everything the row can do that is not the primary verb and not Reject,
  // which has its own control because it asks for a reason first.
  const secondary = VERBS.filter(
    (entry) => entry.verb !== "reject" && entry.verb !== primary?.verb,
  );

  return (
    <div className="job-actions">
      {/* While a rejection reason is being typed, the row is mid-decision and
          the other verbs are not what to do next. They used to stay live, so
          a click landed a status change on a row the operator was in the
          middle of rejecting (review finding on PR #41). */}
      {!asking && (
        <div className="job-actions__row">
          {primary !== null && (
            <button
              type="button"
              className="job-actions__primary"
              disabled={busy || closed}
              onClick={() => {
                change.mutate({ verb: primary.verb });
              }}
            >
              {primary.label}
            </button>
          )}
          {/* Reject keeps its own control rather than sitting behind the
              disclosure. It is the one verb that closes a row and the one
              that asks for a reason, so it is weighted differently from the
              rest. */}
          <button
            type="button"
            className="job-actions__reject"
            disabled={busy || closed}
            onClick={() => {
              setAsking(true);
            }}
          >
            Reject
          </button>
          {onApply !== undefined && (
            <button
              type="button"
              disabled={closed}
              aria-label={`Apply to ${job.company}, ${job.title}`}
              onClick={() => {
                onApply(job);
              }}
            >
              Apply
            </button>
          )}
          <button
            type="button"
            aria-expanded={moreOpen}
            aria-label={`More actions for ${job.company}, ${job.title}`}
            onClick={() => {
              setMoreOpen(!moreOpen);
            }}
          >
            More
          </button>
        </div>
      )}

      {!asking && moreOpen && (
        <div
          className="job-actions__more"
          role="group"
          aria-label={`More actions for ${job.company}, ${job.title}`}
        >
          {secondary.map((entry) => (
            <button
              key={entry.verb}
              type="button"
              disabled={busy || closed}
              onClick={() => {
                change.mutate({ verb: entry.verb });
              }}
            >
              {entry.label}
            </button>
          ))}
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              rescore.mutate();
            }}
          >
            Rescore
          </button>
        </div>
      )}

      {/* The frequent reasons are one click: the pill is the confirmation,
          and Cancel covers a mis-press before it. `other…` reaches the
          free-text input, which stops being the default path (spec 056). */}
      {asking && !otherOpen && (
        <div className="job-actions__pills" role="group" aria-label="Rejection reason">
          <span className="job-actions__pills-label">Reject:</span>
          {FREQUENT_REASONS.map((why) => (
            <button
              key={why}
              type="button"
              className="job-actions__pill"
              disabled={busy}
              onClick={() => {
                change.mutate({ verb: "reject", why });
              }}
            >
              {why}
            </button>
          ))}
          <button
            type="button"
            className="job-actions__pill-other"
            disabled={busy}
            onClick={() => {
              setOtherOpen(true);
            }}
          >
            other…
          </button>
          <button
            type="button"
            className="job-actions__cancel"
            onClick={() => {
              setAsking(false);
            }}
          >
            Cancel
          </button>
        </div>
      )}

      {asking && otherOpen && (
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
            className="job-actions__cancel"
            onClick={() => {
              setAsking(false);
              setOtherOpen(false);
            }}
          >
            Cancel
          </button>
        </span>
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
