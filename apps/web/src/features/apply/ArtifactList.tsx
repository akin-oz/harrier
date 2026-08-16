import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import type { components } from "@harrier/contract";

import { api } from "../../shared/api/client";

const LABELS: Record<string, string> = {
  "resume-pdf": "Tailored resume (PDF)",
  "resume-markdown": "Tailored resume (markdown)",
  "resume-evaluation": "Resume fit evaluation",
  "cover-letter-pdf": "Cover letter (PDF)",
  "cover-letter-markdown": "Cover letter (markdown)",
  answers: "Application answers",
  evaluation: "Offer evaluation",
};

const OPERATION_LABEL: Record<string, string> = {
  resume: "Tailor resume",
  "cover-letter": "Draft cover letter",
  answers: "Draft answers",
  evaluate: "Evaluate offer",
};

// From the contract, never hand-written: an invented field has to be a
// compile error rather than something that silently renders blank (ADR-005).
type ArtifactRow = components["schemas"]["ArtifactOut"];

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
 * URL.
 */
async function openArtifact(jobId: number, kind: string): Promise<void> {
  const { data, error } = await api.GET("/apply/{selector}/artifacts/{kind}", {
    params: { path: { selector: String(jobId), kind } },
    parseAs: "blob",
  });
  if (error !== undefined) {
    throw new Error(`could not open ${kind}: ${JSON.stringify(error)}`);
  }
  const url = URL.createObjectURL(data as Blob);
  globalThis.open(url, "_blank", "noopener");
}

export function ArtifactList({ jobId }: { jobId: number }) {
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

  const present = query.data.filter((item) => item.exists);
  const absent = query.data.filter((item) => !item.exists);

  return (
    <div className="apply-artifacts">
      <h3 className="apply-page__subheading">Artifacts</h3>
      {openError !== null && (
        <p role="alert" className="apply-page__error">
          {openError}
        </p>
      )}
      {present.length === 0 ? (
        <p className="apply-page__muted">Nothing generated for this job yet.</p>
      ) : (
        <ul className="apply-artifacts__list">
          {present.map((item) => (
            <li key={item.kind} className="apply-artifacts__item">
              <span>{LABELS[item.kind] ?? item.kind}</span>
              <button
                type="button"
                onClick={() => {
                  setOpenError(null);
                  openArtifact(jobId, item.kind).catch((error: unknown) => {
                    setOpenError(error instanceof Error ? error.message : String(error));
                  });
                }}
              >
                Open
              </button>
            </li>
          ))}
        </ul>
      )}
      {absent.length > 0 && (
        // Absent kinds are listed rather than hidden: the operator needs to
        // know what would produce them, and an omitted row cannot say that.
        <p className="apply-page__muted apply-artifacts__absent">
          Not generated yet:{" "}
          {absent
            .map(
              (item) =>
                `${LABELS[item.kind] ?? item.kind} (${OPERATION_LABEL[item.produced_by] ?? item.produced_by})`,
            )
            .join(", ")}
          .
        </p>
      )}
    </div>
  );
}
