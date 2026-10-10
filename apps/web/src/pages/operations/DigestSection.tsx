import { useMutation } from "@tanstack/react-query";
import { useState } from "react";

import { TERMINAL_STATES, useRunStream } from "../../features/runs/useRunStream";
import type { EventSourceFactory, RunOut } from "../../features/runs/useRunStream";
import { api } from "../../shared/api/client";
import { RunOutput, TOKEN_REFUSED, refusalMessage } from "./RunOutput";

type Mode = "preview" | "send" | "resend";

/**
 * The daily digest (spec 019): the one button on this page that sends a
 * message to someone. The preview comes first and sends nothing; sending is
 * a separate button marked as sending. A day already delivered is refused
 * in the server's words, and sending it again is its own explicit request.
 */
export function DigestSection({ createEventSource }: { createEventSource: EventSourceFactory }) {
  const stream = useRunStream(createEventSource);
  const [day, setDay] = useState("");
  const [mode, setMode] = useState<Mode | null>(null);
  const [refusedDay, setRefusedDay] = useState<string | null>(null);

  const start = useMutation({
    mutationFn: async (next: Mode): Promise<RunOut> => {
      const { data, error } = await api.POST("/ops/digest", {
        body: {
          dry_run: next === "preview",
          resend: next === "resend",
          date: day === "" ? null : day,
        },
      });
      if (error !== undefined) {
        // Only a send can be refused for a day already delivered; remember
        // the day so "send again" acts on the one the refusal named.
        setRefusedDay(next === "send" ? day : null);
        throw new Error(refusalMessage(error, "the digest was refused"));
      }
      if (data === undefined) throw new Error(TOKEN_REFUSED);
      return data;
    },
    onMutate: () => {
      setRefusedDay(null);
    },
    onSuccess: (data, next) => {
      setMode(next);
      stream.begin(data);
    },
  });

  const busy = stream.active || start.isPending;
  const outcome = digestOutcome(stream, mode);

  return (
    <section className="ops-section" aria-labelledby="ops-digest">
      <h3 id="ops-digest" className="ops-section__heading">
        Daily digest
      </h3>
      <div className="ops-actions">
        <label className="ops-field">
          <span>Day</span>
          <input
            type="date"
            value={day}
            onChange={(event) => {
              setDay(event.target.value);
            }}
          />
        </label>
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            start.mutate("preview");
          }}
        >
          Preview, send nothing
        </button>
        <button
          type="button"
          className="ops-button--caution"
          disabled={busy}
          onClick={() => {
            start.mutate("send");
          }}
        >
          Send to Telegram
        </button>
      </div>
      <p className="ops-note">
        Sending posts a Telegram message to your own chat. With no day chosen, the digest is for
        today.
      </p>
      {start.error !== null && (
        <div role="alert" className="ops-error">
          <p>{start.error.message}</p>
          {refusedDay !== null && (
            <button
              type="button"
              className="ops-button--caution"
              disabled={busy}
              onClick={() => {
                start.mutate("resend");
              }}
            >
              Send it again anyway
            </button>
          )}
        </div>
      )}
      {outcome !== null && (
        <p role="status" className="ops-outcome">
          {outcome}
        </p>
      )}
      <RunOutput stream={stream} label="Digest" failureLine={false} />
    </section>
  );
}

/**
 * Produced, delivered, or neither, from the run's own progress steps: step 1
 * is printed once the digest exists and step 2 once Telegram accepted it
 * (`harrier digest`). The exit status alone cannot tell a failed send from a
 * crash, since both are 1.
 */
export function digestOutcome(
  stream: ReturnType<typeof useRunStream>,
  mode: Mode | null,
): string | null {
  const { run, progress } = stream;
  if (run === null || mode === null || !TERMINAL_STATES.has(run.state)) return null;
  const step = progress?.step ?? 0;
  if (step >= 2) return "Delivered to Telegram.";
  if (step >= 1) {
    return mode === "preview"
      ? "Produced as a preview. Nothing was sent."
      : "Produced and not delivered. The digest is in the log below.";
  }
  return "No digest was produced.";
}
