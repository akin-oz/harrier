import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

import type { Job } from "../entities/job";
import { ApplyPage } from "../pages/apply/ApplyPage";
import { InboxPage } from "../pages/inbox/InboxPage";
import { OutreachPage } from "../pages/outreach/OutreachPage";
import { SettingsPage } from "../pages/settings/SettingsPage";
import { TrackerPage } from "../pages/tracker/TrackerPage";
import { TracksPage } from "../pages/tracks/TracksPage";
import {
  resolveTrack,
  selectTrack,
  trackHref,
  unavailableSentence,
  useSelectedSlug,
  useTracks,
} from "../shared/track";
import type { Track } from "../shared/track";
import "../shared/ui/tokens.css";
import { Header } from "../widgets/header/Header";
import "./App.css";

// Exported so a test can read the cache the app itself fills, and check that
// every key holding track data starts with the track (spec 094).
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: false, refetchOnWindowFocus: false },
  },
});

// `defaultOnly`: the section reads the default track's profile and
// configuration, which no other track has (spec 094).
const SECTIONS = [
  { id: "tracker", label: "Tracker", defaultOnly: false },
  { id: "outreach", label: "Outreach", defaultOnly: true },
  { id: "inbox", label: "Inbox", defaultOnly: true },
  // Last, since it is visited least often (spec 096). The install's own
  // settings, the same on every track.
  { id: "settings", label: "Settings", defaultOnly: false },
] as const;

type Section = (typeof SECTIONS)[number]["id"] | "tracks";

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <Shell />
    </QueryClientProvider>
  );
}

function Shell() {
  // Which page is showing lives here rather than in any page: a page that
  // rendered another page would cross the layer direction FSD keeps one way
  // (ADR-001, and the fsd-reviewer enforces it). There is still no router;
  // the selected track is the URL's `track` query string (spec 094).
  const [section, setSection] = useState<Section>("tracker");
  // The Apply view belongs to a row of one track, so it is held with that
  // track's slug and a switch leaves it behind.
  const [applying, setApplying] = useState<{ job: Job; slug: string | null } | null>(null);
  const slug = useSelectedSlug();
  const tracks = useTracks();
  const track = resolveTrack(tracks.data, slug);
  const applyingHere = applying !== null && applying.slug === slug ? applying.job : null;

  function open(target: Track): void {
    selectTrack(target.is_default ? null : target.slug);
    setApplying(null);
    setSection("tracker");
  }

  return (
    <div className="app-shell">
      <Header
        onManageTracks={() => {
          setApplying(null);
          setSection("tracks");
        }}
      />
      <nav className="app-nav" aria-label="Sections">
        {SECTIONS.map((entry) => {
          const current = section === entry.id && applyingHere === null;
          // Shown, and marked unavailable, rather than hidden: a browser
          // that silently covers less than it does is the defect spec 042
          // names. Still focusable, so pressing it can say why.
          const off = entry.defaultOnly && track != null && !track.is_default;
          return (
            <button
              key={entry.id}
              type="button"
              className={`app-nav__link${current ? " app-nav__link--active" : ""}${off ? " app-nav__link--off" : ""}`}
              aria-current={current ? "page" : undefined}
              aria-disabled={off ? "true" : undefined}
              onClick={() => {
                setApplying(null);
                setSection(entry.id);
              }}
            >
              {entry.label}
            </button>
          );
        })}
      </nav>
      <main className="app-main">
        <Main
          section={section}
          track={track}
          tracksState={tracks}
          slug={slug}
          applying={applyingHere}
          onApply={(job) => {
            setApplying({ job, slug });
          }}
          onBack={() => {
            setApplying(null);
          }}
          onOpen={open}
        />
      </main>
    </div>
  );
}

function Main({
  section,
  track,
  tracksState,
  slug,
  applying,
  onApply,
  onBack,
  onOpen,
}: {
  section: Section;
  track: Track | null | undefined;
  tracksState: ReturnType<typeof useTracks>;
  slug: string | null;
  applying: Job | null;
  onApply: (job: Job) => void;
  onBack: () => void;
  onOpen: (track: Track) => void;
}) {
  if (section === "tracks") return <TracksPage onOpen={onOpen} />;
  if (section === "settings") return <SettingsPage />;
  if (tracksState.isError) {
    return (
      <p role="alert" className="app-notice app-notice--error">
        <span>Could not load tracks: {tracksState.error.message}</span>
        <button
          type="button"
          className="app-notice__button"
          onClick={() => {
            void tracksState.refetch();
          }}
        >
          Retry
        </button>
      </p>
    );
  }
  if (track === undefined) {
    return <p className="app-notice">Loading tracks.</p>;
  }
  if (track === null) {
    // A plain message and the way back, rather than an empty table that
    // looks like a track with nothing in it (spec 094).
    const fallback = tracksState.data?.find((entry) => entry.is_default);
    return (
      <section className="app-notice" aria-labelledby="unknown-track">
        <h2 id="unknown-track" className="app-notice__heading">
          There is no track named “{slug}”
        </h2>
        <p>The address names a track that does not exist. It may have been mistyped.</p>
        <a
          className="app-notice__link"
          href={trackHref(null)}
          onClick={(event) => {
            event.preventDefault();
            selectTrack(null);
          }}
        >
          Go to {fallback?.label ?? "the default track"}
        </a>
      </section>
    );
  }
  const entry = SECTIONS.find((candidate) => candidate.id === section);
  if (entry?.defaultOnly === true && !track.is_default) {
    return <Unavailable heading={entry.label} track={track} />;
  }
  if (applying !== null) return <ApplyPage job={applying} track={track} onBack={onBack} />;
  if (section === "tracker") {
    return <TrackerPage key={track.slug} track={track} onApply={onApply} />;
  }
  return section === "outreach" ? <OutreachPage /> : <InboxPage />;
}

function Unavailable({ heading, track }: { heading: string; track: Track }) {
  return (
    <section className="app-notice" aria-labelledby="unavailable-section">
      <h2 id="unavailable-section" className="app-notice__heading">
        {heading}
      </h2>
      <p>{unavailableSentence(track)}</p>
      <a
        className="app-notice__link"
        href={trackHref(null)}
        onClick={(event) => {
          event.preventDefault();
          selectTrack(null);
        }}
      >
        Switch to the default track
      </a>
    </section>
  );
}
