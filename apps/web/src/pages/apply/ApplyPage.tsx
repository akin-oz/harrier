import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import type { Job } from "../../entities/job";
import { StatusPill } from "../../entities/job";
import { ArtifactList } from "../../features/apply/ArtifactList";
import { TERMINAL_STATES, useRunStream } from "../../features/runs/useRunStream";
import type { EventSourceFactory, RunOut } from "../../features/runs/useRunStream";
import { api } from "../../shared/api/client";
import "../../shared/ui/run.css";
import "./ApplyPage.css";

type Operation = "resume" | "cover-letter" | "answers" | "evaluate";

interface OperationSpec {
  id: Operation;
  /** Names the operation. Selecting is not running, so this is not the
   *  action's name: two controls sharing one accessible name was a real bug
   *  here once. */
  label: string;
  /** Names the action. */
  runLabel: string;
  /** What it spends. Stated on the control rather than left for the operator
   *  to remember, and derived from what the domain function actually does
   *  rather than estimated. */
  cost: string;
  explainer: string;
  input: { field: string; label: string; placeholder: string; required: boolean } | null;
  hasNoAi: boolean;
}

const OPERATIONS: readonly OperationSpec[] = [
  {
    id: "resume",
    label: "Tailor resume",
    runLabel: "Run tailoring",
    cost: "One model call, then a headless browser renders the PDF",
    explainer:
      "Selects and orders content from the verified truth sources, then renders a PDF. The tracker row changes only once that PDF validates.",
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
    runLabel: "Draft the letter",
    cost: "One model call, then a headless browser renders the PDF",
    explainer:
      "Drafts a letter from the application profile and this posting, and validates the PDF.",
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
    runLabel: "Draft the answers",
    cost: "One model call",
    explainer: "Drafts one answer per question from the application profile and this posting.",
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
    runLabel: "Evaluate the offer",
    cost: "One model call",
    explainer:
      "Evaluates a recorded offer for this job. Refuses when there is no offer to evaluate.",
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
  const [expanded, setExpanded] = useState(false);

  const stream = useRunStream(createEventSource);
  const { run, lines, lastLogLine, progress, disconnected, failed } = stream;
  const active = run !== null && !TERMINAL_STATES.has(run.state);

  // A finished run may have written something, so the list is asked again
  // rather than left showing what was true before it started.
  useEffect(() => {
    if (run !== null && TERMINAL_STATES.has(run.state)) {
      void queryClient.invalidateQueries({ queryKey: ["artifacts", job.id] });
    }
  }, [run, queryClient, job.id]);

  // The log opens itself on failure, which is the one case where it is the
  // point, and stays collapsed otherwise so it does not push the artifacts
  // off the screen.
  useEffect(() => {
    if (failed) {
      setExpanded(true);
    }
  }, [failed]);

  const spec = OPERATION_BY_ID[operation];

  const start = useMutation({
    mutationFn: () => startOperation(job.id, operation, text, noAi),
    onSuccess: (data) => {
      setExpanded(false);
      stream.begin(data);
    },
  });

  const missingRequired = spec.input?.required === true && text.trim() === "";
  const percent =
    progress !== null && progress.total !== null && progress.total > 0
      ? Math.round(((progress.step ?? 0) / progress.total) * 100)
      : null;

  const select = (next: Operation) => {
    setOperation(next);
    setText("");
  };

  return (
    <section className="apply-page">
      <div className="apply-page__toolbar">
        <button type="button" className="apply-page__back" onClick={onBack}>
          ← Tracker
        </button>
        <h2 className="apply-page__heading">{job.company}</h2>
        <span className="apply-page__sep">/</span>
        <span className="apply-page__title">{job.title}</span>
        <StatusPill status={job.status} />
      </div>

      <div className="apply-page__layout">
        {/* A single choice, so radios rather than four buttons. As buttons
            they shared an accessible name with the action, which left a
            screen reader with two "Tailor resume" controls doing different
            things. */}
        <div className="apply-page__operations" role="radiogroup" aria-label="Operation">
          {OPERATIONS.map((entry) => (
            <button
              key={entry.id}
              type="button"
              role="radio"
              className={`apply-op${operation === entry.id ? " apply-op--active" : ""}`}
              aria-checked={operation === entry.id}
              // Without this the name would be the label and the cost line
              // read together, which is a sentence rather than a choice.
              aria-label={entry.label}
              onClick={() => {
                select(entry.id);
              }}
            >
              <span className="apply-op__label">{entry.label}</span>
              <span className="apply-op__cost">{entry.cost}</span>
            </button>
          ))}
        </div>

        <div className="apply-panel">
          <div className="apply-panel__head">
            <h3 className="apply-panel__title">{spec.label}</h3>
            <p className="apply-panel__explainer">{spec.explainer}</p>
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
              <span>Deterministic plan only, no model ordering</span>
            </label>
          )}

          <div className="apply-page__actions">
            <button
              type="button"
              className="apply-page__run"
              onClick={() => {
                start.mutate();
              }}
              disabled={active || start.isPending || missingRequired}
            >
              {spec.runLabel}
            </button>
            {run !== null && (
              <span className="apply-page__run-state">
                <span className={`run-dot run-dot--${run.state}`} aria-hidden="true" />
                run {run.id}: <strong>{run.state}</strong>
              </span>
            )}
          </div>

          {missingRequired && (
            <p className="apply-page__muted">Add at least one question to draft.</p>
          )}

          {percent !== null && active && (
            <div className="apply-progress">
              <div className="apply-progress__row">
                <span>{lastLogLine ?? "starting"}</span>
                <span className="apply-progress__pct">{percent}%</span>
              </div>
              <div
                role="progressbar"
                aria-label="Operation progress"
                aria-valuenow={percent}
                aria-valuemin={0}
                aria-valuemax={100}
                className="apply-progress__track"
              >
                <span className="apply-progress__fill" style={{ width: `${String(percent)}%` }} />
              </div>
            </div>
          )}

          {start.error !== null && (
            <p role="alert" className="apply-page__refusal">
              {start.error.message}
            </p>
          )}

          {/* A failed run is where a truth-gate or PDF-gate refusal arrives,
              and it arrives as the last line the process printed. It is shown
              rather than summarised, because the gate's own words are the
              useful part. */}
          {failed && lastLogLine !== null && (
            <p role="alert" className="apply-page__refusal">
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
            <div className="apply-page__log">
              <button
                type="button"
                className="apply-page__log-toggle"
                aria-expanded={expanded}
                onClick={() => {
                  setExpanded(!expanded);
                }}
              >
                {expanded ? "Hide log" : "Show log"}
              </button>
              {expanded && (
                <pre
                  aria-label="operation log"
                  className={failed ? "run-log run-log--failed" : "run-log"}
                >
                  {lines.join("\n")}
                </pre>
              )}
            </div>
          )}
        </div>
      </div>

      <ArtifactList jobId={job.id} onGenerate={select} />
    </section>
  );
}
