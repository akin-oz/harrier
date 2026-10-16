import { useQuery } from "@tanstack/react-query";

import {
  SCHEDULE_KEY,
  fetchSchedule,
  inZone,
  isHosted,
  outcomeWords,
} from "../../features/schedule";
import type { HostedJob, Schedule, ScheduledJob } from "../../features/schedule";

/**
 * When each scheduled job last worked, first on the page because a job that
 * stopped is the failure nothing else announces (spec 029).
 *
 * The page never says a job is healthy. It can say a job's last success is
 * within its cadence, which is a fact the records hold. Whether launchd has
 * the job installed and loaded is not knowable from the server, so the
 * section says so and names the command that reports it (spec 096).
 */
export function ScheduleSection() {
  const schedule = useQuery({ queryKey: SCHEDULE_KEY, queryFn: fetchSchedule });

  return (
    <section className="ops-section" aria-labelledby="ops-schedule">
      <h3 id="ops-schedule" className="ops-section__heading">
        Schedule
      </h3>
      {schedule.isPending && <p className="ops-muted">Reading the schedule…</p>}
      {schedule.isError && (
        <p role="alert" className="ops-error">
          {schedule.error.message}
        </p>
      )}
      {schedule.isSuccess &&
        (isHosted(schedule.data) ? (
          <HostedScheduleBody schedule={schedule.data} />
        ) : (
          <ScheduleBody schedule={schedule.data} />
        ))}
    </section>
  );
}

function ScheduleBody({ schedule }: { schedule: Schedule }) {
  return (
    <>
      {schedule.error !== null && (
        <p role="alert" className="ops-error">
          The schedule definition could not be read: {schedule.error}
        </p>
      )}
      {schedule.jobs.length > 0 && (
        <div className="ops-table-scroll">
          <table className="ops-table">
            <thead>
              <tr>
                <th scope="col">Job</th>
                <th scope="col">Cadence</th>
                <th scope="col">Last success</th>
              </tr>
            </thead>
            <tbody>
              {schedule.jobs.map((job) => (
                <JobRow key={job.name} job={job} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="ops-note">
        {schedule.installed_state} On the host, <code>{schedule.host_command}</code> reports it.
      </p>
    </>
  );
}

function JobRow({ job }: { job: ScheduledJob }) {
  return (
    <tr>
      <th scope="row">{job.name}</th>
      <td>{job.cadence}</td>
      <td>
        {job.records.length === 0 && (
          <span className="ops-muted">No success record is kept for this job yet.</span>
        )}
        <ul className="ops-records">
          {job.records.map((record) => (
            <li key={record.key}>
              {record.overdue ? (
                // Words and a shape, not colour alone.
                <span className="ops-badge ops-badge--overdue">
                  <span className="ops-badge__mark" aria-hidden="true" />
                  Overdue
                </span>
              ) : (
                <span className="ops-badge">Within its cadence</span>
              )}{" "}
              {record.summary}
            </li>
          ))}
        </ul>
      </td>
    </tr>
  );
}

/**
 * Hosted (spec 120): the owner's chosen jobs, the last run the scheduler
 * recorded and the next slot, in the owner's zone. No host command: none is
 * the owner's to run. Next slot is when a slot falls due, not a promise that
 * it starts.
 */
function HostedScheduleBody({ schedule }: { schedule: Schedule }) {
  const zone = schedule.timezone ?? "UTC";
  return (
    <>
      {schedule.error !== null && (
        <p role="alert" className="ops-error">
          Your stored schedule could not be read: {schedule.error}
        </p>
      )}
      {schedule.jobs.length > 0 && (
        <div className="ops-table-scroll">
          <table className="ops-table">
            <thead>
              <tr>
                <th scope="col">Job</th>
                <th scope="col">Cadence</th>
                <th scope="col">Last run</th>
                <th scope="col">Next slot</th>
              </tr>
            </thead>
            <tbody>
              {schedule.jobs.map((job) => (
                <tr key={job.name}>
                  <th scope="row">{job.name}</th>
                  <td>{job.cadence}</td>
                  <td>
                    <LastRun state={job.hosted} zone={zone} />
                  </td>
                  <td>
                    {job.hosted?.next_slot_at == null ? (
                      <span className="ops-muted">None</span>
                    ) : (
                      inZone(job.hosted.next_slot_at, zone)
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="ops-note">{schedule.installed_state}</p>
    </>
  );
}

function LastRun({ state, zone }: { state: HostedJob | null; zone: string }) {
  const outcome = state?.last_outcome ?? null;
  if (state === null || outcome === null) {
    return <span className="ops-muted">No run recorded yet.</span>;
  }
  if (outcome === "running") {
    const since = state.last_started_at ?? state.last_slot_at;
    return (
      <span className="ops-badge">
        Running since {since === null ? "an unknown time" : inZone(since, zone)}
      </span>
    );
  }
  const at = state.last_finished_at;
  const when = at === null ? "" : `, ${inZone(at, zone)}`;
  if (outcome === "ok") return <span className="ops-badge">{`OK${when}`}</span>;
  return (
    // Words and a shape, not colour alone, as "Overdue" is shown.
    <span className="ops-badge ops-badge--overdue">
      <span className="ops-badge__mark" aria-hidden="true" />
      {`${outcomeWords(outcome)}${when}`}
    </span>
  );
}
