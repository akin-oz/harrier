import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import type { components } from "@harrier/contract";

import { api } from "../../shared/api/client";

// From the contract, never hand-written: an invented field has to be a
// compile error rather than something that silently renders blank (ADR-005).
type ArtifactRow = components["schemas"]["ArtifactOut"];

const LABELS: Record<string, string> = {
  "resume-pdf": "Tailored resume",
  "resume-markdown": "Tailored resume (source)",
  "resume-evaluation": "Resume fit evaluation",
  "cover-letter-pdf": "Cover letter",
  "cover-letter-markdown": "Cover letter (source)",
  answers: "Application answers",
  evaluation: "Offer evaluation",
};

const OPERATION_LABEL: Record<string, string> = {
  resume: "Tailor resume",
  "cover-letter": "Draft cover letter",
  answers: "Draft answers",
  evaluate: "Evaluate offer",
};

// Derived from the media type the API reports rather than from the filename,
// which is the operator's own name for the file and not a format.
function formatBadge(mediaType: string): string {
  if (mediaType.startsWith("application/pdf")) return "PDF";
  if (mediaType.startsWith("text/markdown")) return "MD";
  return "FILE";
}

async function fetchArtifacts(jobId: number): Promise<readonly ArtifactRow[]> {
  const { data, error } = await api.GET("/apply/{selector}/artifacts", {
    params: { path: { selector: String(jobId) } },
  });
  if (error !== undefined) {
    throw new Error(`artifacts failed: ${JSON.stringify(error)}`);
  }
  // The contract declares 403, so an absent body is a refusal rather than a
  // shape this can assume away (spec 035).
  if (data === undefined) {
    throw new Error("refused: the local API token was not accepted");
  }
  return data;
}

/**
 * Opening an artifact goes through the client, not a plain link.
 *
 * The route requires the local token (spec 047), and an `href` cannot carry a
 * header. So the bytes are fetched and handed to the browser as an object
 * URL, which is also revoked rather than left to accumulate for the lifetime
 * of the page.
 */
async function openArtifact(jobId: number, kind: string, filename: string): Promise<void> {
  const { data, error } = await api.GET("/apply/{selector}/artifacts/{kind}", {
    params: { path: { selector: String(jobId), kind } },
    parseAs: "blob",
  });
  if (error !== undefined) {
    throw new Error(`could not open ${kind}: ${JSON.stringify(error)}`);
  }
  const url = URL.createObjectURL(data as Blob);
  try {
    // An anchor rather than window.open: it carries the filename the API
    // reported, and a popup blocker does not eat it.
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.rel = "noreferrer";
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    // Revoked on a turn of the event loop, so the click has taken the URL.
    globalThis.setTimeout(() => {
      URL.revokeObjectURL(url);
    }, 0);
  }
}

export function ArtifactList({
  jobId,
  onGenerate,
}: {
  jobId: number;
  /** Selects the operation that produces a missing artifact. It selects
   *  rather than runs: these operations spend money, and a button that
   *  quietly starts one from a list of files would be a surprise. */
  onGenerate?: (operation: "resume" | "cover-letter" | "answers" | "evaluate") => void;
}) {
  const [openError, setOpenError] = useState<string | null>(null);
  const query = useQuery({
    queryKey: ["artifacts", jobId],
    queryFn: () => fetchArtifacts(jobId),
  });

  if (query.isPending) {
    return <p className="apply-page__muted">Looking for artifacts…</p>;
  }
  if (query.isError) {
    return (
      <p role="alert" className="apply-page__error">
        Could not list artifacts: {query.error.message}
      </p>
    );
  }

  const present = query.data.filter((item) => item.exists).length;

  return (
    <section className="apply-artifacts" aria-label="Artifacts">
      <div className="apply-artifacts__head">
        <h3 className="apply-artifacts__heading">Artifacts</h3>
        <span className="apply-artifacts__summary">
          {present === 0
            ? "Nothing generated for this job yet."
            : `${String(present)} of ${String(query.data.length)} generated`}
        </span>
      </div>
      {openError !== null && (
        <p role="alert" className="apply-page__error">
          {openError}
        </p>
      )}
      <div className="apply-artifacts__list">
        {/* Absent rows are listed rather than hidden: the operator needs to
            know what would produce them, and an omitted row cannot say
            that. */}
        {query.data.map((item) => (
          <div
            key={item.kind}
            className={`apply-artifact${item.exists ? "" : " apply-artifact--absent"}`}
          >
            <span className="apply-artifact__main">
              <span className="apply-artifact__dot" aria-hidden="true" />
              <span className="apply-artifact__name">{LABELS[item.kind] ?? item.kind}</span>
              <span className="apply-artifact__format">{formatBadge(item.media_type)}</span>
            </span>
            {item.exists ? (
              <button
                type="button"
                aria-label={`Open ${LABELS[item.kind] ?? item.kind}`}
                onClick={() => {
                  setOpenError(null);
                  openArtifact(jobId, item.kind, item.filename).catch((error: unknown) => {
                    setOpenError(error instanceof Error ? error.message : String(error));
                  });
                }}
              >
                Open
              </button>
            ) : (
              <button
                type="button"
                className="apply-artifact__generate"
                aria-label={`Select ${OPERATION_LABEL[item.produced_by] ?? item.produced_by} to produce ${LABELS[item.kind] ?? item.kind}`}
                onClick={() => {
                  onGenerate?.(
                    item.produced_by as "resume" | "cover-letter" | "answers" | "evaluate",
                  );
                }}
              >
                {OPERATION_LABEL[item.produced_by] ?? item.produced_by}
              </button>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}
