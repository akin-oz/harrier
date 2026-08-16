import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import type { components } from "@harrier/contract";

import { TERMINAL_STATES, useRunStream } from "../../features/runs/useRunStream";
import type { EventSourceFactory, RunOut } from "../../features/runs/useRunStream";
import { api } from "../../shared/api/client";
import "./InboxPage.css";

type MailEvents = components["schemas"]["MailEventsOut"];
type MailEvent = components["schemas"]["MailEventOut"];

// The classifier's own kinds. The label is presentation; which kinds are
// actionable is the domain's decision and arrives on the row.
const KIND_LABEL: Record<string, string> = {
  interview_invite: "Interview invite",
  scheduling_request: "Scheduling request",
  assessment: "Assessment",
  request_info: "Information requested",
  recruiter_reply: "Recruiter reply",
  rejection: "Rejection",
  application_confirmation: "Application confirmation",
  ignored: "Ignored",
};

function refused(error: unknown, fallback: string): string {
  if (typeof error === "object" && error !== null && "detail" in error) {
    const detail = (error as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
  }
  return fallback;
}

async function fetchEvents(): Promise<MailEvents> {
  // This route declares no refusal response, so the generated `error` is
  // `never` and only the body can be absent.
  const { data } = await api.GET("/mail/events", { params: { query: {} } });
  if (data === undefined) throw new Error("could not read the mail archive");
  return data;
}

export function InboxPage({
  createEventSource = (url: string) => new EventSource(url),
}: {
  createEventSource?: EventSourceFactory;
} = {}) {
  const queryClient = useQueryClient();
  const [dryRun, setDryRun] = useState(false);
  const [expanded, setExpanded] = useState(false);

  const events = useQuery({ queryKey: ["mail", "events"], queryFn: fetchEvents });
  const stream = useRunStream(createEventSource);
  const { run, lines, lastLogLine, disconnected, failed } = stream;
  const active = run !== null && !TERMINAL_STATES.has(run.state);

  useEffect(() => {
    if (run !== null && TERMINAL_STATES.has(run.state)) {
      void queryClient.invalidateQueries({ queryKey: ["mail", "events"] });
    }
  }, [run, queryClient]);

  useEffect(() => {
    if (failed) {
      setExpanded(true);
    }
  }, [failed]);

  const watch = useMutation({
    mutationFn: async (): Promise<RunOut> => {
      const { data, error } = await api.POST("/mail/watch", { body: { dry_run: dryRun } });
      if (error !== undefined) throw new Error(refused(error, "could not start the watch"));
      if (data === undefined) throw new Error("refused: the local API token was not accepted");
      return data;
    },
    onSuccess: (data) => {
      setExpanded(false);
      stream.begin(data);
    },
  });

  const actionable = (events.data?.events ?? []).filter((item) => item.actionable);

  return (
    <section className="inbox-page">
      <div className="inbox-page__toolbar">
        <h2 className="inbox-page__heading">Inbox</h2>
        <label className="inbox-page__checkbox">
          <input
            type="checkbox"
            checked={dryRun}
            onChange={(event) => {
              setDryRun(event.target.checked);
            }}
          />
          <span>Dry run, classify and notify nobody</span>
        </label>
        <button
          type="button"
          disabled={active || watch.isPending}
          onClick={() => {
            watch.mutate();
          }}
        >
          Run the watch
        </button>
        {run !== null && (
          <span className="inbox-page__run">
            <span className={`inbox-dot inbox-dot--${run.state}`} aria-hidden="true" />
            run {run.id}: <strong>{run.state}</strong>
          </span>
        )}
      </div>

      <p className="inbox-page__note">
        What the watch classified. The archive keeps the classification and the sender&apos;s
        domain; the subject and the message itself were never stored, so they are not here to show.
        Replies happen in your own mail client.
      </p>

      {watch.error !== null && (
        <p role="alert" className="inbox-page__error">
          {watch.error.message}
        </p>
      )}

      {/* A failed watch is where a missing or expired Gmail token arrives,
          and the domain's own message names the command that repairs it. */}
      {failed && lastLogLine !== null && (
        <p role="alert" className="inbox-page__refusal">
          {lastLogLine}
        </p>
      )}

      {disconnected && (
        <p role="status" className="inbox-page__muted">
          Lost the log stream. The watch may still be going; its state above is refreshed from the
          server.
        </p>
      )}

      {lines.length > 0 && (
        <div className="inbox-page__log">
          <button
            type="button"
            className="inbox-page__log-toggle"
            aria-expanded={expanded}
            onClick={() => {
              setExpanded(!expanded);
            }}
          >
            {expanded ? "Hide log" : "Show log"}
          </button>
          {expanded && (
            <pre
              aria-label="watch log"
              className={failed ? "inbox-log inbox-log--failed" : "inbox-log"}
            >
              {lines.join("\n")}
            </pre>
          )}
        </div>
      )}

      {events.isPending && <p className="inbox-page__muted">Reading the archive…</p>}
      {events.isError && (
        <p role="alert" className="inbox-page__error">
          {events.error.message}
        </p>
      )}

      {events.isSuccess && <Archive data={events.data} actionableCount={actionable.length} />}
    </section>
  );
}

function Archive({ data, actionableCount }: { data: MailEvents; actionableCount: number }) {
  // Three empty lists that mean different things, and the operator needs a
  // different thing from each (spec 049).
  if (!data.has_run) {
    return (
      <p className="inbox-page__empty">
        The watch has not run yet. Run it above to classify recent mail.
      </p>
    );
  }
  if (data.events.length === 0) {
    return (
      <p className="inbox-page__empty">
        The watch has run and classified nothing. Nothing is waiting on you.
      </p>
    );
  }

  return (
    <>
      <p className="inbox-page__summary">
        {actionableCount === 0
          ? "Nothing here needs an action."
          : `${String(actionableCount)} of ${String(data.events.length)} need an action.`}
        {data.at_cap &&
          " The archive is at its size limit, so this is recent history, not all of it."}
      </p>
      <div className="inbox-table-scroll">
        <table className="inbox-table">
          <thead>
            <tr>
              <th scope="col">Kind</th>
              <th scope="col">Company</th>
              <th scope="col">Next action</th>
              <th scope="col">From</th>
              <th scope="col">When</th>
            </tr>
          </thead>
          <tbody>
            {data.events.map((event, index) => (
              <EventRow
                key={`${event.timestamp ?? ""}-${event.company ?? ""}-${String(index)}`}
                event={event}
              />
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function EventRow({ event }: { event: MailEvent }) {
  return (
    <tr className={event.actionable ? "inbox-row inbox-row--actionable" : "inbox-row"}>
      <td>
        <span className={`inbox-kind${event.actionable ? " inbox-kind--actionable" : ""}`}>
          {/* Shape as well as weight, so "needs an action" does not rest on
              colour alone. */}
          <span className="inbox-kind__mark" aria-hidden="true" />
          {KIND_LABEL[event.kind] ?? event.kind}
        </span>
      </td>
      <td>
        <span className="inbox-table__company">{event.company || "unmatched"}</span>
        {event.role !== "" && <span className="inbox-table__role">{event.role}</span>}
      </td>
      <td className="inbox-table__action">{event.next_action || event.ignore_reason}</td>
      <td className="inbox-table__muted">{event.from_domain}</td>
      {/* The archive's own timestamp, trimmed to the minute. Optional in the
          contract because an event written before the field existed has
          none. */}
      <td className="inbox-table__muted">
        {(event.timestamp ?? "").slice(0, 16).replace("T", " ")}
      </td>
    </tr>
  );
}
