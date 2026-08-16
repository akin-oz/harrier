import type { ReactNode } from "react";

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

// Location, source and the added date were three columns that pushed the
// table into a sideways scroll and were never sorted or filtered on. They
// are the same facts, on one line under the job, where they read as context
// rather than as data to compare across rows.
function metaLine(job: Job): string {
  return [job.location, job.source, job.added_at ? `added ${job.added_at}` : ""]
    .filter((part) => part !== "")
    .join("  ·  ");
}

// `renderActions` is passed in rather than imported, because JobTable is an
// entity and the actions are a feature: an entity importing a feature is the
// layering violation `fsd-reviewer` exists to catch. The page owns the wiring
// (spec 042).
export function JobTable({
  jobs,
  emptyMessage,
  renderActions,
  keepOrder = false,
}: {
  jobs: readonly Job[];
  emptyMessage: string;
  renderActions?: (job: Job) => ReactNode;
  keepOrder?: boolean;
}) {
  // Open rows first, then by score. Sorting on score alone reads well until
  // most of the tracker is rejected, and then the first screen fills with
  // closed rows that happened to score highly. A rejected row needs no
  // decision, so it never outranks one that does.
  //
  // `keepOrder` turns that off, because the queue routes answer with the
  // domain's own ranking and re-sorting it here would be this table quietly
  // deciding the question the CLI's `next` already answered (spec 042).
  const rows = useMemo(
    () =>
      keepOrder
        ? [...jobs]
        : [...jobs].sort((a, b) => {
            const closedA = a.status === "rejected" ? 1 : 0;
            const closedB = b.status === "rejected" ? 1 : 0;
            if (closedA !== closedB) {
              return closedA - closedB;
            }
            return (parseScore(b) ?? -1) - (parseScore(a) ?? -1);
          }),
    [jobs, keepOrder],
  );

  if (rows.length === 0) {
    return <p className="job-table-empty">{emptyMessage}</p>;
  }

  const hasActions = renderActions !== undefined;

  return (
    <div className="job-table-scroll">
      {/* Laid out with CSS grid, but still real table elements. The grid gives
          the column alignment and the sticky header; keeping thead, tr and td
          keeps the semantics a screen reader already understands, without
          restating them as role attributes. */}
      <table className={`job-table${hasActions ? "" : " job-table--no-actions"}`}>
        <thead>
          <tr>
            <th scope="col">Status</th>
            <th scope="col">Job</th>
            <th scope="col" className="job-table__num">
              Score
            </th>
            <th scope="col">Next action</th>
            {hasActions && <th scope="col">Actions</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((job) => (
            <tr key={job.id}>
              <td>
                <StatusPill status={job.status} />
              </td>
              <td className="job-table__job">
                <span className="job-table__job-line">
                  <span className="job-table__company">{job.company}</span>
                  {job.url ? (
                    <a
                      className="job-table__title"
                      href={job.url}
                      target="_blank"
                      rel="noreferrer"
                      title={job.title}
                    >
                      {job.title}
                    </a>
                  ) : (
                    <span className="job-table__title" title={job.title}>
                      {job.title}
                    </span>
                  )}
                </span>
                <span className="job-table__meta">{metaLine(job)}</span>
              </td>
              <td className="job-table__num">
                <ScoreBar score={parseScore(job)} />
              </td>
              <td className="job-table__next-action" title={job.next_action}>
                <span className="job-table__clamp">{job.next_action}</span>
              </td>
              {hasActions && <td className="job-table__actions">{renderActions(job)}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
