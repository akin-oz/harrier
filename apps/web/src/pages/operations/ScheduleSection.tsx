import { useQuery } from "@tanstack/react-query";

import type { components } from "@harrier/contract";

import { api } from "../../shared/api/client";

type Schedule = components["schemas"]["ScheduleOut"];
type ScheduledJob = components["schemas"]["ScheduledJobOut"];

async function fetchSchedule(): Promise<Schedule> {
  const { data } = await api.GET("/ops/schedule");
  if (data === undefined) throw new Error("could not read the schedule");
  return data;
}

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
  const schedule = useQuery({ queryKey: ["ops", "schedule"], queryFn: fetchSchedule });

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
      {schedule.isSuccess && <ScheduleBody schedule={schedule.data} />}
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
