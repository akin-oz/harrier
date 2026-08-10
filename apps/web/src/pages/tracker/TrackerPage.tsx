import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { JOB_STATUSES, JobTable } from "../../entities/job";
import type { Job, JobStatus } from "../../entities/job";
import { RunPanel } from "../../features/runs/RunPanel";
import { api } from "../../shared/api/client";
import "./TrackerPage.css";

const STATUS_LABEL: Record<JobStatus, string> = {
  prospect: "Prospect",
  shortlisted: "Shortlisted",
  tailored_cv_requested: "CV requested",
  applied: "Applied",
  interviewing: "Interviewing",
  rejected: "Rejected",
};

// The whole set is fetched once and filtered here. The contract has a status
// query parameter but no search one, and the chip counts have to describe the
// whole tracker rather than the current filter, so one request answers both.
async function fetchAllJobs(): Promise<readonly Job[]> {
  const { data, error } = await api.GET("/jobs", { params: { query: {} } });
  if (error !== undefined) {
    throw new Error(`listJobs failed: ${JSON.stringify(error)}`);
  }
  return data;
}

export function TrackerPage() {
  const [status, setStatus] = useState<JobStatus | "">("");
  const [search, setSearch] = useState("");
  const query = useQuery({ queryKey: ["jobs"], queryFn: fetchAllJobs });

  const counts = useMemo(() => {
    const byStatus = new Map<JobStatus, number>();
    for (const job of query.data ?? []) {
      byStatus.set(job.status, (byStatus.get(job.status) ?? 0) + 1);
    }
    return byStatus;
  }, [query.data]);

  const filtered = useMemo(() => {
    const jobs = query.data ?? [];
    const term = search.trim().toLowerCase();
    return jobs.filter((job) => {
      if (status !== "" && job.status !== status) {
        return false;
      }
      if (term === "") {
        return true;
      }
      return job.company.toLowerCase().includes(term) || job.title.toLowerCase().includes(term);
    });
  }, [query.data, status, search]);

  // An empty tracker and an over-narrow filter are different problems with
  // different fixes, so they do not share a message (spec 026).
  const isFiltered = status !== "" || search.trim() !== "";
  const emptyMessage =
    (query.data?.length ?? 0) === 0
      ? "No jobs yet. Run discovery to find some."
      : isFiltered
        ? "No jobs match this filter."
        : "No jobs to show.";

  return (
    <section className="tracker-page">
      <RunPanel />
      <div className="tracker-page__toolbar">
        <h2 className="tracker-page__heading">Tracker</h2>
        <input
          type="search"
          className="tracker-page__search"
          placeholder="Find a company or title"
          aria-label="Search tracker"
          value={search}
          onChange={(event) => {
            setSearch(event.target.value);
          }}
        />
      </div>
      <div className="tracker-page__filters" role="group" aria-label="Filter by status">
        <button
          type="button"
          className={`tracker-chip${status === "" ? " tracker-chip--active" : ""}`}
          aria-pressed={status === ""}
          onClick={() => {
            setStatus("");
          }}
        >
          All <span className="tracker-chip__count">{query.data?.length ?? 0}</span>
        </button>
        {JOB_STATUSES.map((value) => (
          <button
            key={value}
            type="button"
            className={`tracker-chip${status === value ? " tracker-chip--active" : ""}`}
            aria-pressed={status === value}
            onClick={() => {
              setStatus(value);
            }}
          >
            {STATUS_LABEL[value]}{" "}
            <span className="tracker-chip__count">{counts.get(value) ?? 0}</span>
          </button>
        ))}
      </div>
      {query.isPending && (
        <div className="tracker-page__skeleton" aria-hidden="true">
          {[0, 1, 2, 3, 4, 5].map((row) => (
            <div key={row} className="tracker-page__skeleton-row" />
          ))}
        </div>
      )}
      {query.isError && (
        <p role="alert" className="tracker-page__error">
          <span>Could not load jobs: {query.error.message}</span>
          <button
            type="button"
            className="tracker-page__retry"
            onClick={() => {
              void query.refetch();
            }}
          >
            Retry
          </button>
        </p>
      )}
      {query.isSuccess && <JobTable jobs={filtered} emptyMessage={emptyMessage} />}
    </section>
  );
}
