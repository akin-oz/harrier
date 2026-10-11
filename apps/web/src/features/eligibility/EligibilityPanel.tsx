import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useState } from "react";

import {
  ORIGIN_LABEL,
  ROUTE_LABEL,
  ROUTE_ORDER,
  VERDICT_LABEL,
  VERDICT_ORDER,
  eligibilitySummary,
  isWebLink,
} from "../../entities/job";
import type {
  Eligibility,
  EligibilityEntry,
  EligibilityRoute,
  EligibilityVerdict,
  Job,
} from "../../entities/job";
import { api } from "../../shared/api/client";
import { refusalMessage } from "../../shared/api/refusal";
import { trackKey, trackQuery, useSelectedSlug } from "../../shared/track";
import type { Track } from "../../shared/track";
import "./EligibilityPanel.css";

// The server keeps at most this many characters of an excerpt (spec 122).
export const EXCERPT_LIMIT = 300;

// A write that answered with no body refused the local token (spec 035).
const TOKEN_REFUSED = "the local API token was not accepted";

async function fetchHistory(
  jobId: number,
  slug: string | null,
): Promise<readonly EligibilityEntry[]> {
  const { data, error } = await api.GET("/tracker/{selector}/eligibility", {
    params: { path: { selector: String(jobId) }, query: trackQuery(slug) },
  });
  if (error !== undefined) {
    throw new Error(refusalMessage(error, "could not read the eligibility history"));
  }
  return data;
}

/**
 * A row's Türkiye eligibility (spec 123): the evidence behind the current
 * verdict, every recorded assessment newest first, and, on a writable track,
 * the candidate's own verdict. Read on demand, like the decision history.
 *
 * The form does not restate the server's rules. A refusal, such as a firm
 * verdict with no evidence, comes back in the domain's words and is shown
 * as it came, with what was typed kept.
 */
export function EligibilityPanel({ job, track }: { job: Job; track: Track }) {
  const slug = useSelectedSlug();
  const [open, setOpen] = useState(false);
  const panel = useId();
  const history = useQuery({
    queryKey: trackKey(slug, "jobs", job.id, "eligibility"),
    queryFn: () => fetchHistory(job.id, slug),
    enabled: open,
  });

  return (
    <div className="eligibility-panel">
      <button
        type="button"
        className="eligibility-panel__toggle"
        aria-expanded={open}
        aria-controls={panel}
        onClick={() => {
          setOpen(!open);
        }}
      >
        Eligibility
      </button>
      {open && (
        <div
          id={panel}
          className="eligibility-panel__body"
          role="region"
          aria-label={`Türkiye eligibility of ${job.company}, ${job.title}`}
        >
          <Current eligibility={job.eligibility} history={history.data} />
          {history.isPending && <p className="eligibility-panel__muted">Reading the history…</p>}
          {history.isError && (
            <p role="alert" className="eligibility-panel__error">
              <span>{history.error.message}</span>
              <button
                type="button"
                className="eligibility-panel__retry"
                onClick={() => {
                  void history.refetch();
                }}
              >
                Retry
              </button>
            </p>
          )}
          {history.isSuccess && <History entries={history.data} />}
          {!track.archived && <VerdictForm job={job} />}
        </div>
      )}
    </div>
  );
}

function Evidence({ excerpt, field, url }: { excerpt: string; field: string; url: string }) {
  return (
    <>
      {excerpt !== "" && (
        <p className="eligibility-panel__excerpt">
          <q>{excerpt}</q>
          {field !== "" && <span className="eligibility-panel__field"> in the {field}</span>}
        </p>
      )}
      {url !== "" &&
        (isWebLink(url) ? (
          <a className="eligibility-panel__link" href={url} target="_blank" rel="noreferrer">
            {url}
          </a>
        ) : (
          <span className="eligibility-panel__link">{url}</span>
        ))}
    </>
  );
}

function Current({
  eligibility,
  history,
}: {
  eligibility: Eligibility;
  history: readonly EligibilityEntry[] | undefined;
}) {
  const routes =
    eligibility.routes.length > 0
      ? eligibility.routes.map((route) => ROUTE_LABEL[route]).join(", ")
      : "routes unknown";
  const rules = history?.find((entry) => entry.origin === "rule");
  return (
    <div className="eligibility-panel__current">
      <p className="eligibility-panel__verdict">
        <strong>{VERDICT_LABEL[eligibility.verdict]}</strong>
        <span>{routes}</span>
        <span className="eligibility-panel__who">{ORIGIN_LABEL[eligibility.origin]}</span>
      </p>
      <Evidence
        excerpt={eligibility.excerpt}
        field={eligibility.field}
        url={eligibility.evidence_url}
      />
      {eligibility.checked_at !== "" && (
        <p className="eligibility-panel__muted">
          checked {eligibility.checked_at.replace("T", " ").slice(0, 16)}
          {eligibility.version !== "" && `, rules version ${eligibility.version}`}
        </p>
      )}
      {eligibility.disagrees && (
        <p className="eligibility-panel__differs" role="note">
          The rules&apos; latest assessment differs from yours. Yours stays current.
          {rules !== undefined && ` The rules say: ${eligibilitySummary(rules)}.`}
        </p>
      )}
    </div>
  );
}

function History({ entries }: { entries: readonly EligibilityEntry[] }) {
  if (entries.length === 0) {
    return <p className="eligibility-panel__muted">No assessment is recorded for this job.</p>;
  }
  return (
    <ol className="eligibility-panel__history" aria-label="Eligibility history, newest first">
      {entries.map((entry, index) => (
        <li key={`${entry.at}-${String(index)}`} className="eligibility-panel__entry">
          <span className="eligibility-panel__when">{entry.at.replace("T", " ").slice(0, 16)}</span>
          <span className="eligibility-panel__summary">{eligibilitySummary(entry)}</span>
          <span className="eligibility-panel__who">{ORIGIN_LABEL[entry.origin]}</span>
          <Evidence excerpt={entry.excerpt} field={entry.field} url="" />
          {entry.origin === "rule" && entry.matched !== "" && (
            <details className="eligibility-panel__matched">
              <summary>matched phrases</summary>
              {entry.matched.split("|").join(", ")}
            </details>
          )}
        </li>
      ))}
    </ol>
  );
}

function VerdictForm({ job }: { job: Job }) {
  const slug = useSelectedSlug();
  const queryClient = useQueryClient();
  const formId = useId();
  const counter = `${formId}-left`;
  const [verdict, setVerdict] = useState<EligibilityVerdict | null>(null);
  const [routes, setRoutes] = useState<readonly EligibilityRoute[]>([]);
  const [url, setUrl] = useState("");
  const [excerpt, setExcerpt] = useState("");
  const [checked, setChecked] = useState("");

  const save = useMutation({
    mutationFn: async (chosen: EligibilityVerdict) => {
      const { data, error } = await api.POST("/tracker/{selector}/eligibility", {
        params: { path: { selector: String(job.id) }, query: trackQuery(slug) },
        body: {
          verdict: chosen,
          routes: ROUTE_ORDER.filter((route) => routes.includes(route)),
          evidence_url: url.trim(),
          excerpt,
          // Empty sends nothing, so the server uses now.
          ...(checked === "" ? {} : { checked_at: checked }),
        },
      });
      if (error !== undefined) throw new Error(refusalMessage(error, "the verdict was refused"));
      if (data === undefined) throw new Error(TOKEN_REFUSED);
      return data;
    },
    onSuccess: () => {
      setVerdict(null);
      setRoutes([]);
      setUrl("");
      setExcerpt("");
      setChecked("");
      // The jobs key is a prefix of the history key, so this refreshes both:
      // the row's pill and counts, and this panel's history.
      void queryClient.invalidateQueries({ queryKey: trackKey(slug, "jobs") });
    },
  });

  return (
    <form
      className="eligibility-form"
      aria-label="Record your verdict"
      onSubmit={(event) => {
        event.preventDefault();
        if (verdict !== null) save.mutate(verdict);
      }}
    >
      <fieldset className="eligibility-form__group">
        <legend>Your verdict</legend>
        {VERDICT_ORDER.map((value) => (
          <label key={value} className="eligibility-form__choice">
            <input
              type="radio"
              name={`${formId}-verdict`}
              value={value}
              checked={verdict === value}
              onChange={() => {
                setVerdict(value);
              }}
            />
            {VERDICT_LABEL[value]}
          </label>
        ))}
      </fieldset>
      <fieldset className="eligibility-form__group">
        <legend>Routes the evidence names</legend>
        {ROUTE_ORDER.map((value) => (
          <label key={value} className="eligibility-form__choice">
            <input
              type="checkbox"
              checked={routes.includes(value)}
              onChange={(event) => {
                setRoutes(
                  event.target.checked
                    ? [...routes, value]
                    : routes.filter((route) => route !== value),
                );
              }}
            />
            {ROUTE_LABEL[value]}
          </label>
        ))}
      </fieldset>
      <label className="eligibility-form__field">
        <span>Evidence URL</span>
        <input
          type="text"
          inputMode="url"
          value={url}
          onChange={(event) => {
            setUrl(event.target.value);
          }}
        />
      </label>
      <div className="eligibility-form__field">
        <label htmlFor={`${formId}-excerpt`}>Excerpt</label>
        <textarea
          id={`${formId}-excerpt`}
          maxLength={EXCERPT_LIMIT}
          rows={3}
          aria-describedby={counter}
          value={excerpt}
          onChange={(event) => {
            setExcerpt(event.target.value);
          }}
        />
        <span id={counter} className="eligibility-panel__muted">
          {EXCERPT_LIMIT - excerpt.length} characters left
        </span>
      </div>
      <label className="eligibility-form__field">
        <span>Checked on (optional)</span>
        <input
          type="date"
          value={checked}
          onChange={(event) => {
            setChecked(event.target.value);
          }}
        />
      </label>
      <button type="submit" disabled={verdict === null || save.isPending}>
        Record verdict
      </button>
      {save.error !== null && (
        <p role="alert" className="eligibility-panel__error">
          {save.error.message}
        </p>
      )}
    </form>
  );
}
