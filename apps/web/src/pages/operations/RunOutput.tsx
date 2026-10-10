import type { components } from "@harrier/contract";

import { TERMINAL_STATES, useRunStream } from "../../features/runs/useRunStream";
import "../../shared/ui/run.css";

export type RunStream = ReturnType<typeof useRunStream>;

// What an operations route answers when it refuses, as the contract declares
// it: the domain's words on a 404 or a 409, the hold on a 503, and the field
// errors on a 422. Named from the generated types, so a body the contract
// changes fails the type check here rather than reaching the operator as the
// fallback (spec 082's pattern).
export type Refusal =
  | components["schemas"]["ErrorOut"]
  | components["schemas"]["DatabaseHeldOut"]
  | components["schemas"]["HTTPValidationError"];

export function refusalMessage(error: Refusal, fallback: string): string {
  return typeof error.detail === "string" ? error.detail : fallback;
}

// A route that answered with no body refused the local token; that is the
// only declared outcome without one (spec 035).
export const TOKEN_REFUSED = "refused: the local API token was not accepted";

/**
 * One section's run: its state, the line it failed on, and its whole log.
 *
 * The log is shown rather than summarised. The CLI's own words are the
 * report, and a page that paraphrased them would be a second implementation
 * of it: "nothing is eligible to clear" and a per-source count of rejections
 * already under the current rules are different outcomes, and only the CLI's
 * text keeps them apart (spec 050).
 */
export function RunOutput({
  stream,
  label,
  failureLine = true,
}: {
  stream: RunStream;
  label: string;
  // Whether a failed run's last printed line is its reason. True for a
  // command that ends on its error; the digest ends on the digest itself.
  failureLine?: boolean;
}) {
  const { run, lines, lastLogLine, disconnected, failed } = stream;
  if (run === null) return null;
  const running = !TERMINAL_STATES.has(run.state);
  return (
    <div className="ops-run">
      <p className="ops-run__state" role="status">
        <span className={`run-dot run-dot--${run.state}`} aria-hidden="true" />
        <span>
          {label}: <strong>{run.state}</strong>
          {run.exit_code !== null && run.exit_code !== 0 && ` (exit ${String(run.exit_code)})`}
        </span>
      </p>
      {failureLine && failed && lastLogLine !== null && (
        <p role="alert" className="ops-run__refusal">
          {lastLogLine}
        </p>
      )}
      {disconnected && (
        <p className="ops-muted">
          Lost the log stream. The run may still be going; its state above is refreshed from the
          server.
        </p>
      )}
      {running && lines.length === 0 && <p className="ops-muted">Waiting for the first line…</p>}
      {lines.length > 0 && (
        <pre aria-label={`${label} log`} className={`run-log${failed ? " run-log--failed" : ""}`}>
          {lines.join("\n")}
        </pre>
      )}
    </div>
  );
}
