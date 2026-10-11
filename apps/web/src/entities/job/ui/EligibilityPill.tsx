import { eligibilitySummary } from "../eligibility";
import type { Eligibility } from "../eligibility";
import "./EligibilityPill.css";

// The row's Türkiye verdict and routes, read from the row the page already
// fetched; it makes no request (spec 123). `unknown` shows nothing: it is the
// reading of most rows, and a pill on each would hide the ones that say
// something. The verdict is in the words, so colour is never the only signal.
export function EligibilityPill({ eligibility }: { eligibility: Eligibility }) {
  if (eligibility.verdict === "unknown") return null;
  const yours = eligibility.origin === "candidate";
  return (
    <span className={`eligibility-pill eligibility-pill--${eligibility.verdict}`}>
      <span className="eligibility-pill__mark" aria-hidden="true" />
      {eligibilitySummary(eligibility)}
      {yours && <span className="eligibility-pill__note">your verdict</span>}
      {eligibility.disagrees && (
        <span className="eligibility-pill__note eligibility-pill__note--differs">
          the rules differ
        </span>
      )}
    </span>
  );
}
