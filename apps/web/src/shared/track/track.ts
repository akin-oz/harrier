import type { components } from "@harrier/contract";
import { useQuery } from "@tanstack/react-query";
import { useSyncExternalStore } from "react";

import { api } from "../api/client";

export type Track = components["schemas"]["TrackOut"];
export type TrackKind = components["schemas"]["TrackKindOut"];

// The selected track lives in the page URL, in the same `track` query string
// the API reads, so a reload, a bookmark or a second tab shows the same track
// and the back button returns to the previous one (spec 094). Absent means
// the default track, which is also what the API assumes without it.
const PARAM = "track";

// pushState fires no event of its own, so a switch announces itself under
// this name and every subscriber reads the URL again.
const CHANGE = "harrier:track";

function readSlug(): string | null {
  const value = new URLSearchParams(globalThis.location.search).get(PARAM);
  return value === null || value === "" ? null : value;
}

function subscribe(onChange: () => void): () => void {
  globalThis.addEventListener("popstate", onChange);
  globalThis.addEventListener(CHANGE, onChange);
  return () => {
    globalThis.removeEventListener("popstate", onChange);
    globalThis.removeEventListener(CHANGE, onChange);
  };
}

/** The slug named in the URL, or null for the default track. */
export function useSelectedSlug(): string | null {
  return useSyncExternalStore(subscribe, readSlug, () => null);
}

/** This page's URL with the track set, or removed for the default track. */
export function trackHref(slug: string | null): string {
  const url = new URL(globalThis.location.href);
  if (slug === null) {
    url.searchParams.delete(PARAM);
  } else {
    url.searchParams.set(PARAM, slug);
  }
  return `${url.pathname}${url.search}${url.hash}`;
}

/** Switch track. Changes the URL and nothing else. */
export function selectTrack(slug: string | null): void {
  const href = trackHref(slug);
  const current = `${globalThis.location.pathname}${globalThis.location.search}${globalThis.location.hash}`;
  if (href === current) return;
  globalThis.history.pushState(null, "", href);
  globalThis.dispatchEvent(new Event(CHANGE));
}

/**
 * A query key for data that belongs to a track. It starts with the slug from
 * the URL, so a response fetched for one track is never shown in another and
 * a switch never waits on a stale one (spec 094). The default track without a
 * slug in the URL keys as the empty string.
 */
export function trackKey(slug: string | null, ...parts: readonly unknown[]): unknown[] {
  return [slug ?? "", ...parts];
}

/**
 * The `track` query parameter for a request, or none for the default track,
 * so a request on the default track is the request it was before tracks.
 */
export function trackQuery(slug: string | null): { track?: string } {
  return slug === null ? {} : { track: slug };
}

/** Every track, archived ones included. The same answer on every track. */
export const TRACKS_KEY = ["tracks"] as const;

async function fetchTracks(): Promise<readonly Track[]> {
  const { data, error } = await api.GET("/tracks");
  if (error !== undefined) throw new Error(`listTracks failed: ${JSON.stringify(error)}`);
  return data;
}

export function useTracks() {
  return useQuery({ queryKey: TRACKS_KEY, queryFn: fetchTracks });
}

/**
 * The track the URL names, resolved against the list: `undefined` while the
 * list loads, `null` when the URL names a track that does not exist.
 */
export function resolveTrack(
  tracks: readonly Track[] | undefined,
  slug: string | null,
): Track | null | undefined {
  if (tracks === undefined) return undefined;
  const found =
    slug === null ? tracks.find((track) => track.is_default) : tracks.find((t) => t.slug === slug);
  return found ?? null;
}

/**
 * The word the operator reads for a status on this track, from the kind's own
 * labels (spec 093), with the first letter raised for a control or a pill.
 * The browser holds no label table of its own.
 */
export function statusLabel(track: Track, status: string): string {
  const label = track.status_labels[status] ?? status;
  return label.charAt(0).toUpperCase() + label.slice(1);
}

/** The sentence a section shows when the selected track cannot use it. */
export function unavailableSentence(track: Track): string {
  return `Not available on ${track.label}: it uses the default track's profile and configuration.`;
}
