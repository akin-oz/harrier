import type { JobStatus } from "../types";
import "./StatusPill.css";

// Wording mirrors the pipeline (harrier.tracker.STATUSES). The label carries
// the meaning; colour and mark shape reinforce it rather than being the only
// signal, which is what lets status survive a printout or a colour vision
// deficiency (spec 026).
const STATUS_LABEL: Record<JobStatus, string> = {
  prospect: "Prospect",
  shortlisted: "Shortlisted",
  tailored_cv_requested: "CV requested",
  applied: "Applied",
  interviewing: "Interviewing",
  rejected: "Rejected",
};

export function StatusPill({ status }: { status: JobStatus }) {
  return (
    <span className={`status-pill status-pill--${status}`}>
      <span className="status-pill__mark" aria-hidden="true" />
      {STATUS_LABEL[status]}
    </span>
  );
}
