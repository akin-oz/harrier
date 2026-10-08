import type { components } from "@harrier/contract";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useState } from "react";

import { api } from "../../shared/api/client";
import { TRACKS_KEY } from "../../shared/track";
import type { Track, TrackKind } from "../../shared/track";
import { detailOf } from "./detail";
import "./tracks.css";

type KindName = components["schemas"]["TrackIn"]["kind"];

// The kinds and whether each may be added now, with the domain's reason when
// not (spec 094). The same answer on every track.
const KINDS_KEY = ["track-kinds"] as const;

async function fetchKinds(): Promise<readonly TrackKind[]> {
  // The route declares no error answer; a failed request rejects instead.
  const { data } = await api.GET("/tracks/kinds");
  if (data === undefined) throw new Error("listTrackKinds answered nothing");
  return data;
}

// A slug drafted from the label until the operator edits the slug field:
// lower case, runs of anything else to one hyphen, a letter first.
function draftSlug(label: string): string {
  return label
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^[^a-z]+/, "")
    .replace(/-+$/, "")
    .slice(0, 32)
    .replace(/-+$/, "");
}

/**
 * `harrier tracks add` in the browser. The route calls the same
 * `add_track` (spec 094), so the slug rule, the duplicate check and the
 * refusal of a second industry track are the domain's. A kind the domain
 * refuses is shown, disabled, with its reason, so the form never offers what
 * the API would refuse and never leaves it out unexplained.
 */
export function AddTrack({ onOpen }: { onOpen: (track: Track) => void }) {
  const queryClient = useQueryClient();
  const kinds = useQuery({ queryKey: KINDS_KEY, queryFn: fetchKinds });
  const [label, setLabel] = useState("");
  const [slug, setSlug] = useState("");
  const [slugEdited, setSlugEdited] = useState(false);
  const [kind, setKind] = useState("");
  const [added, setAdded] = useState<Track | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const hintId = useId();

  const available = (kinds.data ?? []).filter((entry) => entry.available);
  // The first kind the domain allows, until the operator picks one.
  const chosen = kind !== "" ? kind : (available[0]?.kind ?? "");

  const add = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/tracks", {
        body: { slug, label: label.trim(), kind: chosen as KindName },
      });
      if (error !== undefined) throw new Error(detailOf(error, "the track was not added"));
      if (data === undefined) throw new Error("the local API token was not accepted");
      return data;
    },
    onSuccess: (track) => {
      setFailure(null);
      setAdded(track);
      setLabel("");
      setSlug("");
      setSlugEdited(false);
      void queryClient.invalidateQueries({ queryKey: TRACKS_KEY });
    },
    onError: (error: Error) => {
      setAdded(null);
      setFailure(error.message);
    },
  });

  return (
    <form
      className="add-track"
      aria-labelledby={`${hintId}-heading`}
      onSubmit={(event) => {
        event.preventDefault();
        add.mutate();
      }}
    >
      <h3 id={`${hintId}-heading`} className="add-track__heading">
        Add a track
      </h3>
      <label className="add-track__field">
        <span>Label</span>
        <input
          value={label}
          required
          maxLength={80}
          onChange={(event) => {
            setLabel(event.target.value);
            if (!slugEdited) setSlug(draftSlug(event.target.value));
          }}
        />
      </label>
      <div className="add-track__field">
        <label htmlFor={`${hintId}-slug-input`}>Slug</label>
        {/* The slug rule as the contract declares it (`TrackIn.slug`); the
            API enforces it and this only lets the browser say so first. */}
        <input
          id={`${hintId}-slug-input`}
          className="add-track__slug"
          value={slug}
          required
          pattern="[a-z][a-z0-9\-]{0,31}"
          maxLength={32}
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          aria-describedby={`${hintId}-slug`}
          onChange={(event) => {
            setSlug(event.target.value);
            setSlugEdited(true);
          }}
        />
        <span id={`${hintId}-slug`} className="add-track__hint">
          Lower-case letters, digits and hyphens, starting with a letter. It appears in page
          addresses and the local server log, so keep it generic.
        </span>
      </div>
      <fieldset className="add-track__kinds">
        <legend>Kind</legend>
        {kinds.isPending && <p className="add-track__hint">Loading kinds.</p>}
        {kinds.isError && (
          <p className="add-track__hint" role="alert">
            Could not load the kinds: {kinds.error.message}
          </p>
        )}
        {(kinds.data ?? []).map((entry) => (
          <label
            key={entry.kind}
            className={`add-track__kind${entry.available ? "" : " add-track__kind--off"}`}
          >
            <input
              type="radio"
              name="kind"
              value={entry.kind}
              checked={chosen === entry.kind}
              disabled={!entry.available}
              aria-describedby={entry.available ? undefined : `${hintId}-${entry.kind}`}
              onChange={() => {
                setKind(entry.kind);
              }}
            />
            <span className="add-track__kind-name">{entry.kind}</span>
            {!entry.available && (
              <span id={`${hintId}-${entry.kind}`} className="add-track__reason">
                Not available: {entry.reason}
              </span>
            )}
          </label>
        ))}
      </fieldset>
      <div className="add-track__actions">
        <button
          type="submit"
          className="tracks-button tracks-button--primary"
          disabled={add.isPending || chosen === "" || label.trim() === "" || slug === ""}
        >
          Add track
        </button>
      </div>
      <div role="status" className="add-track__outcome">
        {added !== null && (
          <>
            <span>Added {added.label}.</span>
            <button
              type="button"
              className="tracks-button"
              onClick={() => {
                onOpen(added);
              }}
            >
              Open {added.label}
            </button>
          </>
        )}
        {failure !== null && <span className="add-track__failure">{failure}</span>}
      </div>
    </form>
  );
}
