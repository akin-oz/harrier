import { useEffect, useId, useRef, useState } from "react";
import type { KeyboardEvent } from "react";

import { resolveTrack, selectTrack, useSelectedSlug, useTracks } from "../../shared/track";
import type { Track } from "../../shared/track";
import "./TrackSwitcher.css";

function Chevron() {
  return (
    <svg className="track-switcher__chevron" viewBox="0 0 12 12" aria-hidden="true">
      <path d="M3 4.5 6 7.5 9 4.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}

function Check() {
  return (
    <svg className="track-switcher__check" viewBox="0 0 12 12" aria-hidden="true">
      <path d="M2.5 6.5 5 9l4.5-6" fill="none" stroke="currentColor" strokeWidth="1.6" />
    </svg>
  );
}

/**
 * Which search the page works in, in the header beside the health badge
 * (spec 094). A menu button: the track's name and kind on the button, the
 * live tracks in the menu with the selected one checked, and a way to the
 * tracks page. Archived tracks are listed there, not here.
 *
 * Keyboard: the button opens the menu on Enter, Space or the arrow keys;
 * arrows, Home and End move through it; Escape closes it and returns focus
 * to the button; Tab closes it and moves on.
 */
export function TrackSwitcher({ onManage }: { onManage: () => void }) {
  const tracks = useTracks();
  const slug = useSelectedSlug();
  const current = resolveTrack(tracks.data, slug);
  const live = (tracks.data ?? []).filter((track) => !track.archived);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const menuId = useId();
  // The live tracks, then "Manage tracks".
  const count = live.length + 1;
  const selectedIndex = Math.max(
    0,
    live.findIndex((track) => track.id === current?.id),
  );

  useEffect(() => {
    if (open) itemRefs.current[active]?.focus();
  }, [open, active]);

  // A press anywhere outside closes the menu without choosing.
  useEffect(() => {
    if (!open) return undefined;
    const onPointer = (event: PointerEvent): void => {
      if (event.target instanceof Node && rootRef.current?.contains(event.target) !== true) {
        setOpen(false);
      }
    };
    document.addEventListener("pointerdown", onPointer);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
    };
  }, [open]);

  function openAt(index: number): void {
    setActive(index);
    setOpen(true);
  }

  function close(): void {
    setOpen(false);
    buttonRef.current?.focus();
  }

  function choose(track: Track): void {
    // The default track is the URL without a slug, as it was before tracks.
    selectTrack(track.is_default ? null : track.slug);
    close();
  }

  function onButtonKey(event: KeyboardEvent<HTMLButtonElement>): void {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      openAt(selectedIndex);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      openAt(count - 1);
    }
  }

  function onMenuKey(event: KeyboardEvent<HTMLUListElement>): void {
    const moves: Record<string, number> = {
      ArrowDown: (active + 1) % count,
      ArrowUp: (active - 1 + count) % count,
      Home: 0,
      End: count - 1,
    };
    const next = moves[event.key];
    if (next !== undefined) {
      event.preventDefault();
      setActive(next);
    } else if (event.key === "Escape") {
      event.preventDefault();
      close();
    } else if (event.key === "Tab") {
      setOpen(false);
    }
  }

  let name: string;
  let kind = "";
  if (tracks.isPending) {
    name = "Loading tracks";
  } else if (tracks.isError) {
    name = "Tracks unavailable";
  } else if (current === null || current === undefined) {
    name = "Unknown track";
  } else {
    name = current.label;
    kind = current.archived ? `${current.kind}, archived` : current.kind;
  }

  return (
    <div className="track-switcher" ref={rootRef}>
      <button
        ref={buttonRef}
        type="button"
        className="track-switcher__button"
        // The visible words, punctuated, so the name reads as a setting and
        // its value rather than as four words run together.
        aria-label={`Track: ${name}${kind !== "" ? `, ${kind}` : ""}`}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        onClick={() => {
          if (open) {
            setOpen(false);
          } else {
            openAt(selectedIndex);
          }
        }}
        onKeyDown={onButtonKey}
      >
        <span className="track-switcher__prefix">Track</span>
        <span className="track-switcher__name">{name}</span>
        {kind !== "" && <span className="track-switcher__kind">{kind}</span>}
        <Chevron />
      </button>
      {open && (
        <ul
          id={menuId}
          role="menu"
          aria-label="Search tracks"
          className="track-switcher__menu"
          onKeyDown={onMenuKey}
        >
          {live.map((track, index) => (
            <li key={track.id} role="none">
              <button
                ref={(element) => {
                  itemRefs.current[index] = element;
                }}
                type="button"
                role="menuitemradio"
                aria-label={`${track.label}, ${track.kind}`}
                aria-checked={track.id === current?.id}
                tabIndex={-1}
                className="track-switcher__item"
                onClick={() => {
                  choose(track);
                }}
              >
                <Check />
                <span className="track-switcher__item-name">{track.label}</span>
                <span className="track-switcher__kind">{track.kind}</span>
              </button>
            </li>
          ))}
          <li role="separator" className="track-switcher__separator" />
          <li role="none">
            <button
              ref={(element) => {
                itemRefs.current[live.length] = element;
              }}
              type="button"
              role="menuitem"
              tabIndex={-1}
              className="track-switcher__item track-switcher__item--manage"
              onClick={() => {
                close();
                onManage();
              }}
            >
              Manage tracks
            </button>
          </li>
        </ul>
      )}
    </div>
  );
}
