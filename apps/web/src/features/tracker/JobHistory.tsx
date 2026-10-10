import { useQuery } from "@tanstack/react-query";
import { useId, useState } from "react";

import type { components } from "@harrier/contract";

import type { Job } from "../../entities/job";
import { api } from "../../shared/api/client";
import { statusLabel, trackKey, trackQuery, useSelectedSlug } from "../../shared/track";
import type { Track } from "../../shared/track";
import "./JobHistory.css";

type JobEvent = components["schemas"]["JobEventOut"];

async function fetchEvents(jobId: number, slug: string | null): Promise<readonly JobEvent[]> {
  const { data, error } = await api.GET("/tracker/{selector}/events", {
    params: { path: { selector: String(jobId) }, query: trackQuery(slug) },
  });
  if (error !== undefined) {
    // The domain's words: a 404 names the selector, a 503 the host hold.
    throw new Error(typeof error.detail === "string" ? error.detail : "could not read the history");
  }
  return data;
}

/**
 * A row's decision history (spec 079, routed by spec 095): every recorded
 * move in order, who made it, the code in the domain's words, and the score
 * the candidate saw. Read on demand, on any track.
 *
 * A backfilled event is marked as reconstructed, so its time is not read as
 * the time of the decision. The moves read in the selected track's own
 * status words (spec 093).
 */
export function JobHistory({ job, track }: { job: Job; track: Track }) {
  const slug = useSelectedSlug();
  const [open, setOpen] = useState(false);
  const panel = useId();
  const events = useQuery({
    queryKey: trackKey(slug, "jobs", job.id, "events"),
    queryFn: () => fetchEvents(job.id, slug),
    enabled: open,
  });

  return (
    <div className="job-history">
      <button
        type="button"
        className="job-history__toggle"
        aria-expanded={open}
        aria-controls={panel}
        onClick={() => {
          setOpen(!open);
        }}
      >
        History
      </button>
      {open && (
        <div id={panel} className="job-history__panel">
          {events.isPending && <p className="job-history__muted">Reading the history…</p>}
          {events.isError && (
            <p role="alert" className="job-history__error">
              {events.error.message}
            </p>
          )}
          {events.isSuccess && events.data.length === 0 && (
            <p className="job-history__muted">No events are recorded for this job.</p>
          )}
          {events.isSuccess && events.data.length > 0 && (
            <ol
              className="job-history__list"
              aria-label={`History of ${job.company}, ${job.title}`}
            >
              {events.data.map((event, index) => (
                <EventLine key={`${event.at}-${String(index)}`} event={event} track={track} />
              ))}
            </ol>
          )}
        </div>
      )}
    </div>
  );
}

function EventLine({ event, track }: { event: JobEvent; track: Track }) {
  const move =
    event.from_status === ""
      ? statusLabel(track, event.to_status)
      : `${statusLabel(track, event.from_status)} → ${statusLabel(track, event.to_status)}`;
  return (
    <li className="job-history__event">
      <span className="job-history__when">{event.at.replace("T", " ").slice(0, 16)}</span>
      <span className="job-history__move">{move}</span>
      <span className="job-history__who">
        {event.kind} by {event.actor}
      </span>
      {event.reason_code !== "" && (
        <span className="job-history__code">{event.reason_label || event.reason_code}</span>
      )}
      {event.reason_text !== "" && <span className="job-history__text">“{event.reason_text}”</span>}
      {event.fit_score !== "" && (
        <span className="job-history__score">score {event.fit_score}</span>
      )}
      {event.backfilled && (
        <span className="job-history__reconstructed">reconstructed, time approximate</span>
      )}
    </li>
  );
}
