import { useMemo } from "react";

import { ScoreBar } from "./ui/ScoreBar";
import { StatusPill } from "./ui/StatusPill";
import type { Job } from "./types";
import "./JobTable.css";

// score and fit_score are strings in the generated contract, and either can
// be blank on a manually added row. Parse defensively rather than trusting
// something that merely looks like a number.
function parseScore(job: Job): number | null {
  const raw = job.score || job.fit_score;
  if (raw === "") {
    return null;
  }
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
}

export function JobTable({ jobs, emptyMessage }: { jobs: readonly Job[]; emptyMessage: string }) {
  // Open rows first, then by score. Sorting on score alone reads well until
  // most of the tracker is rejected, and then the first screen fills with
  // closed rows that happened to score highly. A rejected row needs no
  // decision, so it never outranks one that does.
  const rows = useMemo(
    () =>
      [...jobs].sort((a, b) => {
        const closedA = a.status === "rejected" ? 1 : 0;
        const closedB = b.status === "rejected" ? 1 : 0;
        if (closedA !== closedB) {
          return closedA - closedB;
        }
        return (parseScore(b) ?? -1) - (parseScore(a) ?? -1);
      }),
    [jobs],
  );

  if (rows.length === 0) {
    return <p className="job-table-empty">{emptyMessage}</p>;
  }

  return (
    <div className="job-table-scroll">
      <table className="job-table">
        <thead>
          <tr>
            <th scope="col">Status</th>
            <th scope="col">Company</th>
            <th scope="col">Title</th>
            <th scope="col" className="job-table__num">
              Score
            </th>
            <th scope="col">Next action</th>
            <th scope="col">Location</th>
            <th scope="col">Source</th>
            <th scope="col" className="job-table__num">
              Added
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((job) => (
            <tr key={job.id}>
              <td>
                <StatusPill status={job.status} />
              </td>
              <td className="job-table__company">{job.company}</td>
              <td className="job-table__title" title={job.title}>
                {job.url ? (
                  <a href={job.url} target="_blank" rel="noreferrer">
                    {job.title}
                  </a>
                ) : (
                  <span>{job.title}</span>
                )}
              </td>
              <td className="job-table__num">
                <ScoreBar score={parseScore(job)} />
              </td>
              <td className="job-table__next-action" title={job.next_action}>
                <span className="job-table__clamp">{job.next_action}</span>
              </td>
              <td className="job-table__location" title={job.location}>
                {job.location}
              </td>
              <td className="job-table__muted">{job.source}</td>
              <td className="job-table__muted job-table__num">{job.added_at}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
