import { useId, useState } from "react";
import type { SyntheticEvent } from "react";

import { api } from "../../shared/api/client";
import { refusalMessage } from "../../shared/api/refusal";
import { browserZone, credentialName, inZone, zoneChoices } from "./schedule";
import type { Schedule } from "./schedule";
import "./schedule.css";

/**
 * The owner's choice of scheduled jobs and timezone, the `schedule` kind
 * (spec 120; the kind and its rules are spec 108's).
 *
 * It writes through `PUT /config/schedule`, so the store's rules are the
 * only rules: a refusal is shown in the store's words, which name the field,
 * and the form keeps what was entered. Ticking no job pauses the schedule.
 * `saved` is the set of credential kinds the owner has stored, for the note
 * beside a job that needs one.
 */
export function ScheduleForm({
  schedule,
  saved,
  onSaved,
}: {
  schedule: Schedule;
  saved: ReadonlySet<string>;
  onSaved: () => void;
}) {
  const id = useId();
  const offered = schedule.offered ?? [];
  const [chosen, setChosen] = useState<ReadonlySet<string>>(
    () => new Set(schedule.jobs.map((job) => job.name)),
  );
  const [zone, setZone] = useState(() => schedule.timezone ?? browserZone());
  const [problem, setProblem] = useState<string | null>(null);
  const [savedOnce, setSavedOnce] = useState(false);
  const [busy, setBusy] = useState(false);

  function toggle(name: string, on: boolean): void {
    setChosen((current) => {
      const next = new Set(current);
      if (on) next.add(name);
      else next.delete(name);
      return next;
    });
  }

  async function save(event: SyntheticEvent): Promise<void> {
    event.preventDefault();
    setBusy(true);
    setProblem(null);
    setSavedOnce(false);
    try {
      const { error } = await api.PUT("/config/{kind}", {
        params: { path: { kind: "schedule" } },
        // In the offered order, whatever order the boxes were ticked in.
        body: {
          value: {
            timezone: zone,
            jobs: offered.map((job) => job.name).filter((name) => chosen.has(name)),
          },
        },
      });
      if (error !== undefined) {
        setProblem(refusalMessage(error, "the schedule was not saved"));
        return;
      }
      setSavedOnce(true);
      onSaved();
    } catch (error) {
      // No answer at all: the page says so and the form stays usable.
      setProblem(error instanceof Error ? error.message : "the schedule was not saved");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      className="schedule-form"
      aria-label="Schedule"
      onSubmit={(event) => {
        void save(event);
      }}
    >
      <ScheduleState schedule={schedule} />
      <fieldset className="schedule-form__jobs">
        <legend>Jobs</legend>
        {offered.map((job) => {
          const missing = job.needs.filter((kind) => !saved.has(kind));
          return (
            <div key={job.name} className="schedule-form__job">
              <label>
                <input
                  type="checkbox"
                  checked={chosen.has(job.name)}
                  onChange={(event) => {
                    toggle(job.name, event.target.checked);
                  }}
                />{" "}
                {job.name}
              </label>
              <span className="schedule-form__cadence">{job.cadence}</span>
              {missing.map((kind) => (
                <span key={kind} className="schedule-form__needs">
                  Needs the {credentialName(kind)} credential. Without it, each slot is recorded as
                  not configured and nothing runs.
                </span>
              ))}
            </div>
          );
        })}
      </fieldset>
      <div className="schedule-form__field">
        <label htmlFor={`${id}-zone`}>Timezone</label>
        <select
          id={`${id}-zone`}
          value={zone}
          onChange={(event) => {
            setZone(event.target.value);
          }}
        >
          {zoneChoices(zone).map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      </div>
      <div className="schedule-form__actions">
        <button type="submit" disabled={busy}>
          Save schedule
        </button>
      </div>
      {problem !== null && (
        <p role="alert" className="schedule-form__problem">
          {problem}
        </p>
      )}
      {/* The region is here before the first save, so the answer is announced
          when it arrives (spec 156). */}
      <div role="status">{savedOnce && <Saved schedule={schedule} />}</div>
    </form>
  );
}

function ScheduleState({ schedule }: { schedule: Schedule }) {
  if (schedule.error !== null) {
    return (
      <p role="alert" className="schedule-form__problem">
        The stored schedule could not be read: {schedule.error}. Saving replaces it.
      </p>
    );
  }
  if (schedule.timezone === null) {
    return (
      <p className="schedule-form__state">
        Not scheduled yet: nothing runs on a schedule until you save.
      </p>
    );
  }
  if (schedule.jobs.length === 0) {
    return <p className="schedule-form__state">Paused: no job is chosen.</p>;
  }
  return null;
}

/** The save's answer, and each chosen job's next slot once the page has
 * read the schedule again. It renders into the form's status region. */
function Saved({ schedule }: { schedule: Schedule }) {
  const zone = schedule.timezone;
  return (
    <div className="schedule-form__state">
      <p>Saved. Changes take effect from the next slot.</p>
      {zone !== null && schedule.jobs.length > 0 && (
        <ul className="schedule-form__next">
          {schedule.jobs.map((job) =>
            job.hosted?.next_slot_at == null ? null : (
              <li key={job.name}>
                {job.name}: next slot {inZone(job.hosted.next_slot_at, zone)}
              </li>
            ),
          )}
        </ul>
      )}
    </div>
  );
}
