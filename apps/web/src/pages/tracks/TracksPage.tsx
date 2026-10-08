import { useEffect, useRef, useState } from "react";

import { AddTrack } from "../../features/tracks/AddTrack";
import { ArchiveTrack } from "../../features/tracks/ArchiveTrack";
import { resolveTrack, useSelectedSlug, useTracks } from "../../shared/track";
import type { Track } from "../../shared/track";
import "./TracksPage.css";

function stateOf(track: Track): string {
  if (track.archived) return "Archived";
  return track.is_default ? "Default" : "Live";
}

/**
 * `harrier tracks list|add|archive` in the browser (spec 094): every track
 * with its slug, kind, label and state, archived ones included, and the two
 * writes. `onOpen` switches to a track and shows its tracker.
 */
export function TracksPage({ onOpen }: { onOpen: (track: Track) => void }) {
  const tracks = useTracks();
  const slug = useSelectedSlug();
  const current = resolveTrack(tracks.data, slug);
  const [archived, setArchived] = useState<string | null>(null);
  const noticeRef = useRef<HTMLParagraphElement | null>(null);

  // The Archive button leaves with the track's live state, so focus goes to
  // the sentence that says what happened rather than to nothing.
  useEffect(() => {
    if (archived !== null) noticeRef.current?.focus();
  }, [archived]);

  return (
    <section className="tracks-page" aria-labelledby="tracks-page-heading">
      <h2 id="tracks-page-heading" className="tracks-page__heading">
        Search tracks
      </h2>
      <p className="tracks-page__lede">
        A track is a separate search with its own tracker. The default track also holds the profile,
        the artifacts, outreach and the inbox; any other track has its tracker only.
      </p>
      <p ref={noticeRef} tabIndex={-1} role="status" className="tracks-page__notice">
        {archived}
      </p>
      {tracks.isPending && <p className="tracks-page__lede">Loading tracks.</p>}
      {tracks.isError && (
        <p role="alert" className="tracks-page__error">
          Could not load tracks: {tracks.error.message}
        </p>
      )}
      {tracks.isSuccess && (
        <ul className="tracks-list" aria-label="Tracks">
          {tracks.data.map((track) => {
            const selected = track.id === current?.id;
            return (
              <li
                key={track.id}
                className={`tracks-list__item${track.archived ? " tracks-list__item--archived" : ""}`}
              >
                <div className="tracks-list__name">
                  <span className="tracks-list__label">{track.label}</span>
                  <span className="tracks-list__slug">{track.slug}</span>
                </div>
                <span className="tracks-list__kind">{track.kind}</span>
                <span className="tracks-list__state">
                  {stateOf(track)}
                  {selected && <span className="tracks-list__selected">, selected</span>}
                </span>
                <div className="tracks-list__actions">
                  {!selected && (
                    <button
                      type="button"
                      className="tracks-button"
                      aria-label={`Open ${track.label}`}
                      onClick={() => {
                        onOpen(track);
                      }}
                    >
                      Open
                    </button>
                  )}
                  {!track.is_default && !track.archived && (
                    <ArchiveTrack
                      track={track}
                      onArchived={(done) => {
                        setArchived(`Archived ${done.label}. Its rows stay readable.`);
                      }}
                    />
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
      <AddTrack onOpen={onOpen} />
    </section>
  );
}
