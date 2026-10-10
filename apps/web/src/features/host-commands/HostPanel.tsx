import type { ReactNode } from "react";

import { CopyCommand } from "./CopyCommand";
import { useCommandPlaces, useHostFacts } from "./useCommandPlaces";
import type { HostFact, HostFacts } from "./useCommandPlaces";
import "./hostCommands.css";

const TITLES: Record<HostFact, string> = {
  schedule: "The schedule",
  gmail_token: "The mail token",
  model: "The learned score",
  image: "The running image",
  database_owner: "Who owns the database",
  profile: "Profile documents on disk",
};

function daysSince(timestamp: string, now: Date): number | null {
  const moment = new Date(timestamp);
  if (Number.isNaN(moment.getTime())) return null;
  return Math.max(0, Math.floor((now.getTime() - moment.getTime()) / 86_400_000));
}

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
  profileCount,
  now,
}: {
  fact: HostFact;
  facts: HostFacts;
  profileCount: number | null;
  now: Date;
}) {
  if (fact === "schedule") {
    if (facts.schedule_definition !== "present") {
      return (
        <p className="host-fact host-fact--absent">
          {facts.schedule_definition === "absent"
            ? "No schedule definition is in the checkout."
            : "The schedule definition in the checkout cannot be read."}
        </p>
      );
    }
    return (
      <>
        <ul className="host-jobs">
          {facts.schedule.map((job) => {
            const days = job.last_success_at === null ? null : daysSince(job.last_success_at, now);
            return (
              <li key={job.name} className="host-jobs__job">
                <span className="host-jobs__name">{job.name}</span>
                <span className="host-jobs__cadence">{job.cadence}</span>
                <span className="host-jobs__success">
                  {job.last_success_at === null
                    ? "has never recorded a success here"
                    : days === null
                      ? `last success time is unreadable (${job.last_success_at})`
                      : `last succeeded ${ago(days)}`}
                </span>
              </li>
            );
          })}
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
export function HostPanel({
  profileCount = null,
  now = new Date(),
}: {
  profileCount?: number | null;
  now?: Date;
}) {
  const places = useCommandPlaces();
  const facts = useHostFacts();

  if (places.isPending || facts.isPending) {
    return <p className="host-muted">Reading what the container can see…</p>;
  }
  if (places.isError || facts.isError) {
    return (
      <p role="alert" className="host-error">
        {(places.error ?? facts.error)?.message}
      </p>
    );
  }

  return (
    <ul className="host-panel">
      {places.data.panel.map((row) => (
        <li key={row.fact} className="host-panel__row">
          <h4 className="host-panel__title">{TITLES[row.fact]}</h4>
          <div className="host-panel__facts">
            <Fact fact={row.fact} facts={facts.data} profileCount={profileCount} now={now} />
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
