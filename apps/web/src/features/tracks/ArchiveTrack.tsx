import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";

import { api } from "../../shared/api/client";
import { TRACKS_KEY } from "../../shared/track";
import type { Track } from "../../shared/track";
import { detailOf } from "./detail";
import "./tracks.css";

/**
 * `harrier tracks archive` in the browser (spec 094). Archiving cannot be
 * undone, so the button asks first, on the row rather than in a modal: the
 * question names the track and says what archiving does. Focus goes to
 * Cancel, the safe answer; Escape is Cancel; either returns focus to the
 * Archive button. A refusal is announced here in the domain's words; a
 * success goes to `onArchived`, because this control leaves the row with
 * the track's live state and the page has to say what happened.
 */
export function ArchiveTrack({
  track,
  onArchived,
}: {
  track: Track;
  onArchived: (track: Track) => void;
}) {
  const queryClient = useQueryClient();
  const [asking, setAsking] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [refocus, setRefocus] = useState(false);
  const openerRef = useRef<HTMLButtonElement | null>(null);
  const cancelRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (asking) cancelRef.current?.focus();
  }, [asking]);

  useEffect(() => {
    if (!refocus) return;
    openerRef.current?.focus();
    setRefocus(false);
  }, [refocus]);

  const archive = useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/tracks/{slug}/archive", {
        params: { path: { slug: track.slug } },
      });
      if (error !== undefined) throw new Error(detailOf(error, "the track was not archived"));
      if (data === undefined) throw new Error("the local API token was not accepted");
      return data;
    },
    onSuccess: (archived) => {
      setAsking(false);
      onArchived(archived);
      void queryClient.invalidateQueries({ queryKey: TRACKS_KEY });
    },
    onError: (error: Error) => {
      setAsking(false);
      setMessage(error.message);
      setRefocus(true);
      void queryClient.invalidateQueries({ queryKey: TRACKS_KEY });
    },
  });

  function cancel(): void {
    setAsking(false);
    setRefocus(true);
  }

  function onKey(event: KeyboardEvent<HTMLDivElement>): void {
    if (event.key !== "Escape") return;
    event.preventDefault();
    cancel();
  }

  return (
    <div className="archive-track">
      {asking ? (
        <div
          className="archive-track__confirm"
          role="group"
          aria-label={`Archive ${track.label}`}
          onKeyDown={onKey}
        >
          <p className="archive-track__question">
            Archive {track.label}? Its rows stay readable and take no new changes. This cannot be
            undone.
          </p>
          <div className="archive-track__buttons">
            <button
              type="button"
              className="tracks-button tracks-button--danger"
              disabled={archive.isPending}
              onClick={() => {
                archive.mutate();
              }}
            >
              Archive {track.label}
            </button>
            <button
              ref={cancelRef}
              type="button"
              className="tracks-button"
              disabled={archive.isPending}
              onClick={cancel}
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <button
          ref={openerRef}
          type="button"
          className="tracks-button"
          aria-label={`Archive ${track.label}`}
          onClick={() => {
            setMessage(null);
            setAsking(true);
          }}
        >
          Archive
        </button>
      )}
      <p role="status" className="archive-track__message">
        {message}
      </p>
    </div>
  );
}
