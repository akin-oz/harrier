import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api } from "../../shared/api/client";
import { refusalMessage } from "../../shared/api/refusal";
import type { Refusal } from "../../shared/api/refusal";
import { trackKey } from "../../shared/track";
import { forgetCaptureFragment } from "./fragment";
import type { CaptureFields, Captured } from "./fragment";
import "./CapturePage.css";

const FALLBACK = "The job was not added.";

// A duplicate or a missing field answers in the domain's own words, and any
// other refusal the API declares carries a `detail`. A body that is not the
// API's JSON (a proxy's error page, an empty answer) arrives as a string or
// nothing, and reads as the fallback.
function refusalOf(error: unknown): string {
  if (typeof error !== "object" || error === null) return FALLBACK;
  if ("message" in error && typeof error.message === "string") return error.message;
  return "detail" in error ? refusalMessage(error as Refusal, FALLBACK) : FALLBACK;
}

// A job handed over from a posting, by the extension or any link to the
// capture fragment (spec 119). Nothing is written until the owner presses
// Add, so a page that crafts the link can only fill a form.
export function CapturePage({
  captured,
  trackLabel,
  onClose,
}: {
  captured: Captured;
  trackLabel: string;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [fields, setFields] = useState<CaptureFields>(captured.fields);
  const [added, setAdded] = useState(false);
  const [outcome, setOutcome] = useState<string | null>(null);

  // The fields are held here now. Read once, the fragment leaves the
  // address bar, and a reload does not capture again.
  useEffect(() => {
    forgetCaptureFragment();
  }, []);

  const add = useMutation({
    mutationFn: async (body: CaptureFields) => {
      const { error, response } = await api.POST("/capture/add", { body });
      // The status decides, not the error: the client hands back no error at
      // all for an empty failed answer (review of PR #413).
      if (!response.ok) throw new Error(refusalOf(error));
    },
    onSuccess: () => {
      setAdded(true);
      setOutcome(`Added to ${trackLabel}.`);
      // Capture writes to the default track, which keys as no slug.
      void queryClient.invalidateQueries({ queryKey: trackKey(null, "jobs") });
    },
    onError: (error: Error) => {
      setOutcome(error.message);
    },
  });

  function field(name: keyof CaptureFields, value: string): void {
    setFields((current) => ({ ...current, [name]: value }));
  }

  const incomplete = fields.title.trim() === "" || fields.company.trim() === "";

  return (
    <section className="capture-page" aria-labelledby="capture-heading">
      <h2 id="capture-heading" className="capture-page__heading">
        Add a job from a posting
      </h2>
      <p className="capture-page__track">Adds to {trackLabel}.</p>
      {added ? (
        <button type="button" className="capture-page__button" onClick={onClose}>
          Open the tracker
        </button>
      ) : (
        <form
          className="capture-page__form"
          onSubmit={(event) => {
            event.preventDefault();
            add.mutate(fields);
          }}
        >
          <label className="capture-page__field">
            Title
            <input
              value={fields.title}
              onChange={(event) => {
                field("title", event.target.value);
              }}
            />
          </label>
          <label className="capture-page__field">
            Company
            <input
              value={fields.company}
              onChange={(event) => {
                field("company", event.target.value);
              }}
            />
          </label>
          <label className="capture-page__field">
            Location
            <input
              value={fields.location}
              onChange={(event) => {
                field("location", event.target.value);
              }}
            />
          </label>
          <label className="capture-page__field">
            Link
            <input
              inputMode="url"
              value={fields.url}
              onChange={(event) => {
                field("url", event.target.value);
              }}
            />
          </label>
          {captured.linkLeftOut && (
            <p className="capture-page__note">
              The link was left out because it was not a web address.
            </p>
          )}
          <label className="capture-page__field">
            Description
            <textarea
              rows={12}
              value={fields.description}
              onChange={(event) => {
                field("description", event.target.value);
              }}
            />
          </label>
          <p className="capture-page__source">Source: {fields.source}</p>
          <div className="capture-page__buttons">
            <button
              type="submit"
              className="capture-page__button capture-page__button--primary"
              disabled={incomplete || add.isPending}
            >
              Add to tracker
            </button>
            <button type="button" className="capture-page__button" onClick={onClose}>
              Cancel
            </button>
          </div>
        </form>
      )}
      {/* In the page before any message arrives, so the message is spoken
          when it does (spec 156). */}
      <p className="capture-page__outcome" role="status">
        {outcome}
      </p>
    </section>
  );
}
