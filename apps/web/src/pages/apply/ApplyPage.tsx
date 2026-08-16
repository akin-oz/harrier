import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import type { Job } from "../../entities/job";
import { ArtifactList } from "../../features/apply/ArtifactList";
import { TERMINAL_STATES, useRunStream } from "../../features/runs/useRunStream";
import type { EventSourceFactory, RunOut } from "../../features/runs/useRunStream";
import { api } from "../../shared/api/client";
import "./ApplyPage.css";

type Operation = "resume" | "cover-letter" | "answers" | "evaluate";

interface OperationSpec {
  id: Operation;
  label: string;
  /** What the free-text box holds, or null when the operation takes none. */
  input: { field: string; label: string; placeholder: string; required: boolean } | null;
  hasNoAi: boolean;
}

const OPERATIONS: readonly OperationSpec[] = [
  {
    id: "resume",
    label: "Tailor resume",
    input: {
      field: "jd_text",
      label: "Job description (optional)",
      placeholder: "Leave empty to use the description captured at import.",
      required: false,
    },
    hasNoAi: true,
  },
  {
    id: "cover-letter",
    label: "Draft cover letter",
    input: {
      field: "notes",
      label: "Extra guidance (optional)",
      placeholder: "Anything the letter should emphasise.",
      required: false,
    },
    hasNoAi: false,
  },
  {
    id: "answers",
    label: "Draft answers",
    input: {
      field: "questions",
      label: "Questions, one per line",
      placeholder: "Why do you want to work here?",
      required: true,
    },
    hasNoAi: false,
  },
  {
    id: "evaluate",
    label: "Evaluate offer",
    input: {
      field: "jd_text",
      label: "Job description (optional)",
      placeholder: "Leave empty to use the description captured at import.",
      required: false,
    },
    hasNoAi: false,
  },
];

// A total lookup, so selecting an operation cannot land on undefined.
const OPERATION_BY_ID: Record<Operation, OperationSpec> = Object.fromEntries(
  OPERATIONS.map((entry) => [entry.id, entry]),
) as Record<Operation, OperationSpec>;

function refusalMessage(error: unknown, operation: Operation): string {
  // The API's own words, not a status code translated back into prose. A
  // refusal here is a domain outcome the operator has to read (spec 047).
  if (typeof error === "object" && error !== null && "detail" in error) {
    const detail = (error as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
  }
  return `could not start ${operation}: ${JSON.stringify(error)}`;
}

function started(data: RunOut | undefined): RunOut {
  if (data === undefined) {
    throw new Error("refused: the local API token was not accepted");
  }
  return data;
}

/**
 * Each operation posts its own body shape, typed by the contract.
 *
 * One switch rather than an index into a path map with a cast: the bodies
 * genuinely differ, and casting them to a common shape would let an invented
 * field through the one seam that is supposed to reject it (ADR-005).
 */
async function startOperation(
  jobId: number,
  operation: Operation,
  text: string,
  noAi: boolean,
): Promise<RunOut> {
  const path = { selector: String(jobId) };
  if (operation === "resume") {
    const { data, error } = await api.POST("/apply/{selector}/resume", {
      params: { path },
      body: { jd_text: text, no_ai: noAi },
    });
    if (error !== undefined) throw new Error(refusalMessage(error, operation));
    return started(data);
  }
  if (operation === "cover-letter") {
    const { data, error } = await api.POST("/apply/{selector}/cover-letter", {
      params: { path },
      body: { notes: text },
    });
    if (error !== undefined) throw new Error(refusalMessage(error, operation));
    return started(data);
  }
  if (operation === "answers") {
    const { data, error } = await api.POST("/apply/{selector}/answers", {
      params: { path },
      body: { questions: text },
    });
    if (error !== undefined) throw new Error(refusalMessage(error, operation));
    return started(data);
  }
  const { data, error } = await api.POST("/apply/{selector}/evaluate", {
    params: { path },
    body: { jd_text: text },
  });
  if (error !== undefined) throw new Error(refusalMessage(error, operation));
  return started(data);
}

export function ApplyPage({
  job,
  onBack,
  createEventSource = (url: string) => new EventSource(url),
}: {
  job: Job;
  onBack: () => void;
  createEventSource?: EventSourceFactory;
}) {
  const queryClient = useQueryClient();
  const [operation, setOperation] = useState<Operation>("resume");
  const [text, setText] = useState("");
  const [noAi, setNoAi] = useState(false);

  const stream = useRunStream(createEventSource);
  const { run, lines, lastLogLine, disconnected, failed } = stream;
  const active = run !== null && !TERMINAL_STATES.has(run.state);

  // A finished run may have written something, so the list is asked again
  // rather than left showing what was true before it started.
  useEffect(() => {
    if (run !== null && TERMINAL_STATES.has(run.state)) {
      void queryClient.invalidateQueries({ queryKey: ["artifacts", job.id] });
    }
  }, [run, queryClient, job.id]);

  const spec = OPERATION_BY_ID[operation];

  const start = useMutation({
    mutationFn: () => startOperation(job.id, operation, text, noAi),
    onSuccess: (data) => {
      stream.begin(data);
    },
  });

  const missingRequired = spec.input?.required === true && text.trim() === "";

  return (
    <section className="apply-page">
      <div className="apply-page__toolbar">
        <button type="button" className="apply-page__back" onClick={onBack}>
          ← Tracker
        </button>
        <h2 className="apply-page__heading">
          {job.company}: {job.title}
        </h2>
      </div>

      {/* A single choice, so radios rather than four buttons. As buttons they
          shared an accessible name with the action below, which left a screen
          reader with two "Tailor resume" controls doing different things. */}
      <div className="apply-page__operations" role="radiogroup" aria-label="Operation">
        {OPERATIONS.map((entry) => (
          <button
            key={entry.id}
            type="button"
            role="radio"
            className={`apply-chip${operation === entry.id ? " apply-chip--active" : ""}`}
            aria-checked={operation === entry.id}
            onClick={() => {
              setOperation(entry.id);
              setText("");
            }}
          >
            {entry.label}
          </button>
        ))}
      </div>

      {spec.input && (
        <label className="apply-page__field">
          <span>{spec.input.label}</span>
          <textarea
            value={text}
            rows={4}
            placeholder={spec.input.placeholder}
            onChange={(event) => {
              setText(event.target.value);
            }}
          />
        </label>
      )}

      {spec.hasNoAi && (
        <label className="apply-page__checkbox">
          <input
            type="checkbox"
            checked={noAi}
            onChange={(event) => {
              setNoAi(event.target.checked);
            }}
          />
          <span>Deterministic plan only (no AI ordering)</span>
        </label>
      )}

      <div className="apply-page__actions">
        <button
          type="button"
          onClick={() => {
            start.mutate();
          }}
          disabled={active || start.isPending || missingRequired}
        >
          {spec.label}
        </button>
        {run !== null && (
          <span className="apply-page__run">
            <span className={`run-dot run-dot--${run.state}`} aria-hidden="true" />
            run {run.id}: <strong>{run.state}</strong>
          </span>
        )}
      </div>

      {missingRequired && <p className="apply-page__muted">Add at least one question to draft.</p>}

      {start.error !== null && (
        <p role="alert" className="apply-page__error">
          {start.error.message}
        </p>
      )}

      {/* A failed run is where a truth-gate or PDF-gate refusal arrives, and
          it arrives as the last line the CLI printed. It is shown rather than
          summarised, because the gate's own words are the useful part. */}
      {failed && lastLogLine !== null && (
        <p role="alert" className="apply-page__error apply-page__refusal">
          {lastLogLine}
        </p>
      )}

      {disconnected && (
        <p role="status" className="apply-page__muted">
          Lost the log stream. The run may still be going; its state above is refreshed from the
          server.
        </p>
      )}

      {lines.length > 0 && (
        <pre aria-label="run log" className={`run-log${failed ? " run-log--failed" : ""}`}>
          {lines.join("\n")}
        </pre>
      )}

      <ArtifactList jobId={job.id} />
    </section>
  );
}
