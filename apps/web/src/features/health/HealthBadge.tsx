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
  return (
    <span className="health-badge">
      {health.demo && <span className="health-badge__tag">DEMO</span>}
      <span>{health.database}</span>
      <span className="health-badge__dot" aria-hidden="true" />
      <span>{health.job_count} jobs</span>
    </span>
  );
}
