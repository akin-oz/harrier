import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import type { components } from "@harrier/contract";

import { api } from "../../shared/api/client";
import { refusalMessage } from "../../shared/api/refusal";
import { trackKey, useSelectedSlug } from "../../shared/track";
import "./DescriptionPanel.css";

type Description = components["schemas"]["DescriptionOut"];

/**
 * One job's stored description (spec 149): what discovery captured, which
 * every application operation reads. Shown when there is one, and taken by
 * paste when there is none, so the operator pastes only what the system
 * does not already hold.
 */
export function DescriptionPanel({ jobId }: { jobId: number }) {
  const slug = useSelectedSlug();
  const queryClient = useQueryClient();
  const key = trackKey(slug, "description", jobId);

  const stored = useQuery({
    queryKey: key,
    queryFn: async (): Promise<Description> => {
      const { data, error } = await api.GET("/apply/{selector}/description", {
        params: { path: { selector: String(jobId) } },
      });
      if (error !== undefined) {
        throw new Error(refusalMessage(error, "could not read the description"));
      }
      return data;
    },
  });

  // The paste box's text, or null while it is closed. Kept on a refusal, so
  // the operator does not paste again.
  const [draft, setDraft] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [saved, setSaved] = useState(false);
  // Where focus goes when the box opens or closes: the control that opened
  // or replaced it is unmounted, and focus must not fall to the page.
  const box = useRef<HTMLTextAreaElement>(null);
  const replace = useRef<HTMLButtonElement>(null);
  const [focusTarget, setFocusTarget] = useState<"box" | "replace" | null>(null);
  useEffect(() => {
    if (focusTarget === "box") box.current?.focus();
    if (focusTarget === "replace") replace.current?.focus();
    if (focusTarget !== null) setFocusTarget(null);
  }, [focusTarget]);

  const save = useMutation({
    mutationFn: async (text: string): Promise<Description> => {
      const { data, error } = await api.PUT("/apply/{selector}/description", {
        params: { path: { selector: String(jobId) } },
        body: { text },
      });
      if (error !== undefined) {
        throw new Error(refusalMessage(error, "the description was refused"));
      }
      return data;
    },
    onSuccess: (data) => {
      queryClient.setQueryData(key, data);
      setDraft(null);
      setShowAll(false);
      setSaved(true);
      setFocusTarget("replace");
    },
  });

  const heading = (
    <h3 id="description-heading" className="description-panel__heading">
      Job description
    </h3>
  );

  if (stored.isError) {
    return (
      <section className="description-panel" aria-labelledby="description-heading">
        {heading}
        <p role="alert" className="description-panel__error">
          {stored.error.message}
        </p>
      </section>
    );
  }
  if (!stored.isSuccess) {
    return <p className="description-panel__note">Reading the description…</p>;
  }

  const editing = draft !== null || !stored.data.stored;

  return (
    <section className="description-panel" aria-labelledby="description-heading">
      {heading}
      {!stored.data.stored && (
        <p className="description-panel__note">
          No description was captured for this job. Paste it to use it for every operation.
        </p>
      )}

      {stored.data.stored && draft === null && (
        <>
          <div
            role="group"
            className={
              showAll
                ? "description-panel__text"
                : "description-panel__text description-panel__text--collapsed"
            }
            aria-label="Stored description"
          >
            {stored.data.text}
          </div>
          {saved && (
            <p role="status" className="description-panel__note">
              Saved. Every operation reads this description.
            </p>
          )}
          <div className="description-panel__actions">
            <button
              type="button"
              aria-expanded={showAll}
              onClick={() => {
                setShowAll(!showAll);
              }}
            >
              {showAll ? "Show less" : "Show all"}
            </button>
            <button
              ref={replace}
              type="button"
              onClick={() => {
                setDraft(stored.data.text);
                setSaved(false);
                setFocusTarget("box");
              }}
            >
              Replace
            </button>
          </div>
        </>
      )}

      {editing && (
        <form
          className="description-panel__form"
          onSubmit={(event) => {
            event.preventDefault();
            save.mutate(draft ?? "");
          }}
        >
          <label className="description-panel__field">
            <span>Paste the job description</span>
            <textarea
              ref={box}
              rows={8}
              value={draft ?? ""}
              onChange={(event) => {
                setDraft(event.target.value);
              }}
            />
          </label>
          {save.error !== null && (
            <p role="alert" className="description-panel__error">
              {save.error.message}
            </p>
          )}
          <div className="description-panel__actions">
            <button
              type="submit"
              className="description-panel__save"
              disabled={save.isPending || (draft ?? "").trim() === ""}
            >
              Save
            </button>
            {stored.data.stored && (
              <button
                type="button"
                onClick={() => {
                  setDraft(null);
                  save.reset();
                  setFocusTarget("replace");
                }}
              >
                Cancel
              </button>
            )}
          </div>
        </form>
      )}
    </section>
  );
}
