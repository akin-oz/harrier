import type { ReactNode } from "react";

import { CopyCommand } from "./CopyCommand";
import { useCommandPlaces, useHostFacts, useSchedule } from "./useCommandPlaces";
import type { HostFact, HostFacts, Schedule } from "./useCommandPlaces";
import "./hostCommands.css";

const TITLES: Record<HostFact, string> = {
  schedule: "The schedule",
  gmail_token: "The mail token",
  model: "The learned score",
  image: "The running image",
  database_owner: "Who owns the database",
  profile: "Profile documents on disk",
};

function ago(days: number): string {
  if (days === 0) return "today";
  return days === 1 ? "1 day ago" : `${String(days)} days ago`;
}

// Said where the container cannot know, in the same words everywhere, and
// drawn without any healthy colour (spec 096).
function NotKnowable({ children }: { children: ReactNode }) {
  return <p className="host-fact host-fact--unknown">{children}</p>;
}

function Fact({
  fact,
  facts,
  schedule,
  profileCount,
}: {
  fact: HostFact;
  facts: HostFacts;
  schedule: Schedule;
  profileCount: number | null;
}) {
  if (fact === "schedule") {
    // Spec 050's schedule read: the same records the Operations page shows.
    if (schedule.error !== null) {
      return (
        <p className="host-fact host-fact--absent">
          The schedule definition cannot be read: {schedule.error}
        </p>
      );
    }
    return (
      <>
        <ul className="host-jobs">
          {schedule.jobs.map((job) => (
            <li key={job.name} className="host-jobs__job">
              <span className="host-jobs__name">{job.name}</span>
              <span className="host-jobs__cadence">{job.cadence}</span>
              {job.records.map((record) => (
                <span key={record.key} className="host-jobs__success">
                  {record.overdue ? "Overdue: " : ""}
                  {record.summary}
                </span>
              ))}
            </li>
          ))}
        </ul>
        <NotKnowable>
          Whether each job is installed and loaded is the host&apos;s to report.
        </NotKnowable>
      </>
    );
  }
  if (fact === "gmail_token") {
    const token = facts.gmail_token;
    if (token.state === "present") {
      return (
        <p className="host-fact">
          The token file is present
          {token.age_days === null ? "." : `, written ${ago(token.age_days)}.`}
        </p>
      );
    }
    return (
      <p className="host-fact host-fact--absent">
        {token.state === "absent"
          ? "No token file is present."
          : "No token file is configured for this server."}
      </p>
    );
  }
  if (fact === "model") {
    const model = facts.model;
    const exported =
      facts.newest_feature_export === null
        ? "No feature export yet."
        : `Newest feature export: ${facts.newest_feature_export}.`;
    if (model.state === "active") {
      return (
        <p className="host-fact">
          Active model trained {model.trained_at ?? "on an unrecorded date"}, version{" "}
          <code>{model.version}</code>. {exported}
        </p>
      );
    }
    return (
      <p className="host-fact host-fact--absent">
        {model.state === "missing"
          ? "No trained model is active; the rules score every job."
          : "The model file is refused; the rules score every job."}{" "}
        {exported}
      </p>
    );
  }
  if (fact === "image") {
    return (
      <>
        <p className="host-fact">
          {facts.image_revision === "unknown"
            ? "This server does not know its revision."
            : `This server runs revision ${facts.image_revision}.`}
        </p>
        <NotKnowable>Whether that matches the checkout is the host&apos;s to know.</NotKnowable>
      </>
    );
  }
  if (fact === "database_owner") {
    return <NotKnowable>Not knowable from inside the container.</NotKnowable>;
  }
  return (
    <p className="host-fact">
      {profileCount === null
        ? "The profile documents are listed above."
        : `${String(profileCount)} profile documents are stored; they are listed above.`}
    </p>
  );
}

/**
 * What only the host can do: each fact the container can read, beside the
 * exact command that would change it (spec 096). Nothing here runs a
 * command; each one is copied and run in a terminal on the host.
 */
export function HostPanel({ profileCount = null }: { profileCount?: number | null }) {
  const places = useCommandPlaces();
  const facts = useHostFacts();
  const schedule = useSchedule();

  if (places.isPending || facts.isPending || schedule.isPending) {
    return <p className="host-muted">Reading what the container can see…</p>;
  }
  if (places.isError || facts.isError || schedule.isError) {
    return (
      <p role="alert" className="host-error">
        {(places.error ?? facts.error ?? schedule.error)?.message}
      </p>
    );
  }

  return (
    <ul className="host-panel">
      {places.data.panel.map((row) => (
        <li key={row.fact} className="host-panel__row">
          <h4 className="host-panel__title">{TITLES[row.fact]}</h4>
          <div className="host-panel__facts">
            <Fact
              fact={row.fact}
              facts={facts.data}
              schedule={schedule.data}
              profileCount={profileCount}
            />
          </div>
          <div className="host-panel__commands">
            {row.commands.map((command) => (
              <CopyCommand key={command} command={command} />
            ))}
          </div>
        </li>
      ))}
    </ul>
  );
}
