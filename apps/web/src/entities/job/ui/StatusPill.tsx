import type { JobStatus } from "../types";
import "./StatusPill.css";

// The label carries the meaning; colour and mark shape reinforce it rather
// than being the only signal, which is what lets status survive a printout or
// a colour vision deficiency (spec 026). The words come from the track's kind
// (spec 093), passed in, so the pill holds no label table of its own and an
// academic row reads "Submitted" where an industry row reads "Applied"
// (spec 094). The class stays the stored status: the pipeline stage is the
// same on every track.
export function StatusPill({ status, label }: { status: JobStatus; label: string }) {
  return (
    <span className={`status-pill status-pill--${status}`}>
      <span className="status-pill__mark" aria-hidden="true" />
      {label}
    </span>
  );
}
