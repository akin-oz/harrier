import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import type { components } from "@harrier/contract";

import { api } from "../../shared/api/client";
import { refusalMessage } from "../../shared/api/refusal";
import { trackKey, useSelectedSlug } from "../../shared/track";
import "./BriefPanel.css";

type Brief = components["schemas"]["BriefOut"];

const EMPTY: Brief = {
  never_name: [],
  guidance_url: "",
  employer_guidance: "",
  letter: { max_words: null, max_sentences: null, paragraphs: null },
  answers: { max_words: null, max_sentences: null },
  evidence: [],
  views: {},
  compensation_number: "",
  confirmed_skills: [],
};

/** What the form holds: text as typed, so a refusal keeps the input. */
interface Draft {
  neverName: string;
  guidanceUrl: string;
  employerGuidance: string;
  letterWords: string;
  letterSentences: string;
  letterParagraphs: string;
  answerWords: string;
  answerSentences: string;
  evidence: string;
  views: { question: string; view: string }[];
  compensation: string;
  // Not edited here, and sent back as it came, so saving never drops it.
  confirmedSkills: string[];
}

function toDraft(brief: Brief): Draft {
  const number = (value: number | null | undefined): string =>
    value === null || value === undefined ? "" : String(value);
  return {
    neverName: brief.never_name.join("\n"),
    guidanceUrl: brief.guidance_url,
    employerGuidance: brief.employer_guidance,
    letterWords: number(brief.letter.max_words),
    letterSentences: number(brief.letter.max_sentences),
    letterParagraphs: number(brief.letter.paragraphs),
    answerWords: number(brief.answers.max_words),
    answerSentences: number(brief.answers.max_sentences),
    evidence: brief.evidence.join("\n"),
    views: Object.entries(brief.views).map(([question, view]) => ({ question, view })),
    compensation: brief.compensation_number,
    confirmedSkills: [...brief.confirmed_skills],
  };
}

function lines(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line !== "");
}

/**
 * The whole brief as the store reads it. A limit left empty is left out
 * rather than sent as nothing; any other value is sent as typed, so a
 * wrong one is refused in the store's words rather than corrected here.
 */
export function fromDraft(draft: Draft): Record<string, unknown> {
  const limits = (entries: [string, string][]): Record<string, unknown> => {
    const result: Record<string, unknown> = {};
    for (const [key, raw] of entries) {
      if (raw.trim() === "") continue;
      const value = Number(raw);
      result[key] = Number.isFinite(value) ? value : raw;
    }
    return result;
  };
  const views: Record<string, string> = {};
  for (const entry of draft.views) {
    if (entry.question.trim() !== "") views[entry.question.trim()] = entry.view;
  }
  return {
    never_name: lines(draft.neverName),
    guidance_url: draft.guidanceUrl,
    employer_guidance: draft.employerGuidance,
    letter: limits([
      ["max_words", draft.letterWords],
      ["max_sentences", draft.letterSentences],
      ["paragraphs", draft.letterParagraphs],
    ]),
    answers: limits([
      ["max_words", draft.answerWords],
      ["max_sentences", draft.answerSentences],
    ]),
    evidence: lines(draft.evidence),
    views,
    compensation_number: draft.compensation,
    confirmed_skills: draft.confirmedSkills,
  };
}

/**
 * One job's application brief (spec 066, routed by spec 095): shown as
 * fields, saved whole in one request, and refused in the store's words with
 * the operator's input kept in the form.
 */
export function BriefPanel({ jobId }: { jobId: number }) {
  const slug = useSelectedSlug();
  const queryClient = useQueryClient();
  const key = trackKey(slug, "brief", jobId);

  const stored = useQuery({
    queryKey: key,
    queryFn: async (): Promise<Brief | null> => {
      const { data, error, response } = await api.GET("/apply/{selector}/brief", {
        params: { path: { selector: String(jobId) } },
      });
      // No brief yet is an empty form, not a failure.
      if (response.status === 404) return null;
      if (error !== undefined) throw new Error(refusalMessage(error, "could not read the brief"));
      return data ?? null;
    },
  });

  const [draft, setDraft] = useState<Draft | null>(null);
  useEffect(() => {
    if (stored.isSuccess && draft === null) setDraft(toDraft(stored.data ?? EMPTY));
  }, [stored.isSuccess, stored.data, draft]);

  const save = useMutation({
    mutationFn: async (body: Record<string, unknown>): Promise<Brief> => {
      const { data, error } = await api.PUT("/apply/{selector}/brief", {
        params: { path: { selector: String(jobId) } },
        body,
      });
      if (error !== undefined) throw new Error(refusalMessage(error, "the brief was refused"));
      if (data === undefined) throw new Error("refused: the local API token was not accepted");
      return data;
    },
    onSuccess: (data) => {
      queryClient.setQueryData(key, data);
      setDraft(toDraft(data));
    },
  });

  if (stored.isError) {
    return (
      <section className="brief-panel" aria-labelledby="brief-heading">
        <h3 id="brief-heading" className="brief-panel__heading">
          Brief
        </h3>
        <p role="alert" className="brief-panel__error">
          {stored.error.message}
        </p>
      </section>
    );
  }
  if (draft === null) {
    return <p className="brief-panel__muted">Reading the brief…</p>;
  }

  function edit(change: Partial<Draft>): void {
    setDraft((current) => (current === null ? current : { ...current, ...change }));
  }

  return (
    <section className="brief-panel" aria-labelledby="brief-heading">
      <h3 id="brief-heading" className="brief-panel__heading">
        Brief
      </h3>
      <p className="brief-panel__note">
        What you know about this application that no truth document holds. Letters and answers
        follow it.
        {stored.data === null && " Nothing is stored for this job yet."}
      </p>
      <form
        className="brief-panel__form"
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate(fromDraft(draft));
        }}
      >
        <label className="brief-field">
          <span>Names never to write, one per line</span>
          <textarea
            rows={2}
            value={draft.neverName}
            onChange={(event) => {
              edit({ neverName: event.target.value });
            }}
          />
        </label>
        <fieldset className="brief-limits">
          <legend>Letter limits</legend>
          <NumberField
            label="Letter words"
            value={draft.letterWords}
            onChange={(value) => {
              edit({ letterWords: value });
            }}
          />
          <NumberField
            label="Letter sentences"
            value={draft.letterSentences}
            onChange={(value) => {
              edit({ letterSentences: value });
            }}
          />
          <NumberField
            label="Letter paragraphs"
            value={draft.letterParagraphs}
            onChange={(value) => {
              edit({ letterParagraphs: value });
            }}
          />
        </fieldset>
        <fieldset className="brief-limits">
          <legend>Answer limits</legend>
          <NumberField
            label="Answer words"
            value={draft.answerWords}
            onChange={(value) => {
              edit({ answerWords: value });
            }}
          />
          <NumberField
            label="Answer sentences"
            value={draft.answerSentences}
            onChange={(value) => {
              edit({ answerSentences: value });
            }}
          />
        </fieldset>
        <label className="brief-field">
          <span>Employer guidance</span>
          <textarea
            rows={3}
            value={draft.employerGuidance}
            onChange={(event) => {
              edit({ employerGuidance: event.target.value });
            }}
          />
        </label>
        <label className="brief-field">
          <span>Guidance URL</span>
          <input
            type="url"
            value={draft.guidanceUrl}
            onChange={(event) => {
              edit({ guidanceUrl: event.target.value });
            }}
          />
        </label>
        <label className="brief-field">
          <span>Evidence, one entry per line</span>
          <textarea
            rows={3}
            value={draft.evidence}
            onChange={(event) => {
              edit({ evidence: event.target.value });
            }}
          />
        </label>
        <fieldset className="brief-views">
          <legend>Views per question</legend>
          {draft.views.map((entry, index) => (
            <div key={index} className="brief-views__row">
              <label className="brief-field">
                <span>Question {index + 1}</span>
                <input
                  value={entry.question}
                  onChange={(event) => {
                    edit({
                      views: draft.views.map((item, at) =>
                        at === index ? { ...item, question: event.target.value } : item,
                      ),
                    });
                  }}
                />
              </label>
              <label className="brief-field">
                <span>Your view</span>
                <textarea
                  rows={2}
                  value={entry.view}
                  onChange={(event) => {
                    edit({
                      views: draft.views.map((item, at) =>
                        at === index ? { ...item, view: event.target.value } : item,
                      ),
                    });
                  }}
                />
              </label>
              <button
                type="button"
                onClick={() => {
                  edit({ views: draft.views.filter((_, at) => at !== index) });
                }}
              >
                Remove question {index + 1}
              </button>
            </div>
          ))}
          <button
            type="button"
            onClick={() => {
              edit({ views: [...draft.views, { question: "", view: "" }] });
            }}
          >
            Add a question
          </button>
        </fieldset>
        <label className="brief-field">
          <span>Compensation number</span>
          <input
            value={draft.compensation}
            onChange={(event) => {
              edit({ compensation: event.target.value });
            }}
          />
        </label>
        {save.error !== null && (
          <p role="alert" className="brief-panel__error">
            {save.error.message}
          </p>
        )}
        {save.isSuccess && (
          <p role="status" className="brief-panel__muted">
            Brief saved.
          </p>
        )}
        <button type="submit" className="brief-panel__save" disabled={save.isPending}>
          Save the brief
        </button>
      </form>
    </section>
  );
}

function NumberField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="brief-field brief-field--number">
      <span>{label}</span>
      <input
        inputMode="numeric"
        value={value}
        onChange={(event) => {
          onChange(event.target.value);
        }}
      />
    </label>
  );
}
