import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { JOB_STATUSES, JobTable } from "../../entities/job";
import type { Job, JobStatus } from "../../entities/job";
import { RunPanel } from "../../features/runs/RunPanel";
import { AddJob } from "../../features/tracker/AddJob";
import { JobActions } from "../../features/tracker/JobActions";
import { JobHistory } from "../../features/tracker/JobHistory";
import { api } from "../../shared/api/client";
import {
  statusLabel,
  trackKey,
  trackQuery,
  unavailableSentence,
  useSelectedSlug,
} from "../../shared/track";
import type { Track } from "../../shared/track";
import "./TrackerPage.css";

// `next` and `review` are the CLI's two orderings and they answer different
// questions: what to work on, and what still needs a decision. They are a
// separate control from the status chips because the ordering is the answer,
// and a chip that also reordered the table would hide that (spec 042).
const VIEWS = [
  { id: "all", label: "All" },
  { id: "next", label: "Next up" },
  { id: "review", label: "Needs a decision" },
] as const;

type View = (typeof VIEWS)[number]["id"];

// The whole set is fetched once and filtered here. The contract has a status
// query parameter but no search one, and the chip counts have to describe the
// whole tracker rather than the current filter, so one request answers both.
async function fetchAllJobs(slug: string | null): Promise<readonly Job[]> {
  const { data, error } = await api.GET("/jobs", { params: { query: trackQuery(slug) } });
  if (error !== undefined) {
    throw new Error(`listJobs failed: ${JSON.stringify(error)}`);
  }
  return data;
}

// The queue ordering is computed by the domain, not re-derived here: ranking
// the rows in the browser would be the second implementation this spec exists
// to prevent.
async function fetchQueue(slug: string | null, undecided: boolean): Promise<readonly Job[]> {
  const { data, error } = await api.GET("/tracker/queue", {
    params: { query: { undecided, ...trackQuery(slug) } },
  });
  if (error !== undefined) {
    throw new Error(`queue failed: ${JSON.stringify(error)}`);
  }
  return data;
}

// `track` is the selected track, resolved by the app from the URL. Its kind
// names the statuses and decides what the table leads with; whether it is
// the default decides what the rows may do (spec 094). The page is keyed by
// track where it is mounted, so a switch starts it fresh.
export function TrackerPage({ track, onApply }: { track: Track; onApply?: (job: Job) => void }) {
  const slug = useSelectedSlug();
  const [status, setStatus] = useState<JobStatus | "">("");
  const [search, setSearch] = useState("");
  // An academic track opens on its queue: the server orders it by the
  // nearest deadline, which is the question the day starts with.
  const [view, setView] = useState<View>(track.is_default ? "all" : "next");
  const label = (value: JobStatus): string => statusLabel(track, value);
  // An archived track keeps its rows readable and takes no writes, so it
  // offers none (spec 093).
  const writable = !track.archived;

  const all = useQuery({
    queryKey: trackKey(slug, "jobs"),
    queryFn: () => fetchAllJobs(slug),
  });
  const queue = useQuery({
    queryKey: trackKey(slug, "jobs", "queue", view),
    queryFn: () => fetchQueue(slug, view === "review"),
    enabled: view !== "all",
  });
  const query = view === "all" ? all : queue;

  const counts = useMemo(() => {
    const byStatus = new Map<JobStatus, number>();
    for (const job of all.data ?? []) {
      byStatus.set(job.status, (byStatus.get(job.status) ?? 0) + 1);
    }
    return byStatus;
  }, [all.data]);

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
      ? view === "all"
        ? track.is_default
          ? "No jobs yet. Run discovery to find some."
          : "Nothing on this track yet. Add a position by hand."
        : "Nothing in this queue."
      : isFiltered
        ? "No jobs match this filter."
        : "No jobs to show.";

  return (
    <section className="tracker-page">
      {/* Runs start on the default track only: no run kind is on the shared
          allowlist yet (spec 094, Honest limitations). */}
      {track.is_default ? (
        <RunPanel />
      ) : (
        <p className="tracker-page__unavailable">
          <strong>Runs.</strong> {unavailableSentence(track)}
        </p>
      )}
      {track.archived && (
        <p className="tracker-page__archived" role="note">
          {track.label} is archived. Its rows can be read and not changed.
        </p>
      )}
      <div className="tracker-page__toolbar">
        <h2 className="tracker-page__heading">
          {track.label}
          <span className="tracker-page__kind">
            {track.kind} track{track.archived ? ", archived" : ""}
          </span>
        </h2>
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
        {writable && <AddJob track={track} />}
      </div>
      <div className="tracker-page__filters" role="group" aria-label="Queue">
        {VIEWS.map((entry) => (
          <button
            key={entry.id}
            type="button"
            className={`tracker-chip${view === entry.id ? " tracker-chip--active" : ""}`}
            aria-pressed={view === entry.id}
            onClick={() => {
              setView(entry.id);
            }}
          >
            {entry.label}
          </button>
        ))}
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
          All <span className="tracker-chip__count">{all.data?.length ?? 0}</span>
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
            {label(value)} <span className="tracker-chip__count">{counts.get(value) ?? 0}</span>
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
      {query.isSuccess && (
        <JobTable
          jobs={filtered}
          emptyMessage={emptyMessage}
          statusLabel={label}
          keepOrder={view !== "all"}
          deadlineLed={!track.is_default}
          renderDetails={(job) => <JobHistory job={job} track={track} />}
          renderActions={
            writable ? (job) => <JobActions job={job} track={track} onApply={onApply} /> : undefined
          }
        />
      )}
    </section>
  );
}
