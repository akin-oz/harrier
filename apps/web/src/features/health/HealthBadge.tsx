import { useQuery } from "@tanstack/react-query";

import type { components } from "@harrier/contract";

import { api } from "../../shared/api/client";
import "./HealthBadge.css";

type HealthOut = components["schemas"]["HealthOut"];

async function fetchHealth(): Promise<HealthOut> {
  // /health declares no error responses, so the generated client types its
  // error as never: guarding on it the way the other callers do would be
  // dead code, but data is still optional, and undefined here means the
  // response was not the shape the contract promises.
  const { data } = await api.GET("/health");
  if (data === undefined) {
    throw new Error("getHealth returned no body");
  }
  return data;
}

// Answers "is the machine working?" at the level of "can I even reach it",
// which is a different failure than a run failing after it started.
export function HealthBadge() {
  const query = useQuery({ queryKey: ["health"], queryFn: fetchHealth, retry: false });
  if (query.isPending) {
    return <span className="health-badge health-badge--idle">checking…</span>;
  }
  if (query.isError) {
    return <span className="health-badge health-badge--error">API unreachable</span>;
  }
  const health = query.data;
  // An image is stale until rebuilt, so a container answering correctly while
  // running older code than the checkout is the failure spec 051 exists to
  // remove. Showing the revision is what makes that visible to the operator
  // rather than only to whoever thinks to curl /health. "unknown" is what a
  // process started outside an image build reports, which `just dev` is, so it
  // is rendered as a plain label rather than as a fault.
  const stamped = health.revision !== "unknown";
  // While a host process holds the database, the container refuses it and
  // there is no count to show. Saying which command holds it and since when
  // is what tells the operator the pages answering 503 are waiting, not
  // broken (spec 075). UTC from the ISO string, so it reads the same anywhere.
  const hold = health.database_hold;
  return (
    <span className="health-badge">
      {health.demo && <span className="health-badge__tag">DEMO</span>}
      <span>{health.database}</span>
      <span className="health-badge__dot" aria-hidden="true" />
      {hold ? (
        <span className="health-badge__hold" title={`held since ${hold.since}`}>
          held by host: {hold.subcommand} since {holdTime(hold.since)}
        </span>
      ) : (
        <span>{health.job_count ?? 0} jobs</span>
      )}
      <span className="health-badge__dot" aria-hidden="true" />
      <span
        className="health-badge__revision"
        title={stamped ? `built ${health.built_at}` : "not built from an image"}
      >
        {stamped ? health.revision : "dev"}
      </span>
    </span>
  );
}

function holdTime(since: string): string {
  // "2026-10-05T07:00:00+00:00" -> "07:00 UTC". Anything else is shown as is.
  const time = /T(\d{2}:\d{2})/.exec(since)?.[1];
  return time === undefined ? since : `${time} UTC`;
}
