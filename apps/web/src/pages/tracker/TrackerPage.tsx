import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { JOB_STATUSES, JobTable } from "../../entities/job";
import type { Job, JobStatus } from "../../entities/job";
import { RunPanel } from "../../features/runs/RunPanel";
import { api } from "../../shared/api/client";

async function fetchJobs(status: JobStatus | ""): Promise<readonly Job[]> {
  const { data, error } = await api.GET("/jobs", {
    params: { query: status ? { status } : {} },
  });
  if (error !== undefined) {
    throw new Error(`listJobs failed: ${JSON.stringify(error)}`);
  }
  return data;
}

export function TrackerPage() {
  const [status, setStatus] = useState<JobStatus | "">("");
  const query = useQuery({
    queryKey: ["jobs", status],
    queryFn: () => fetchJobs(status),
  });

  return (
    <section>
      <RunPanel />
      <h2>Tracker</h2>
      <label>
        Status{" "}
        <select
          value={status}
          onChange={(event) => {
            setStatus(event.target.value as JobStatus | "");
          }}
        >
          <option value="">all</option>
          {JOB_STATUSES.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
      </label>
      {query.isPending && <p>Loading jobs…</p>}
      {query.isError && <p role="alert">Could not load jobs: {query.error.message}</p>}
      {query.isSuccess && <JobTable jobs={query.data} />}
    </section>
  );
}
