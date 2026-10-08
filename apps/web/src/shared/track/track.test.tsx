import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test } from "vitest";

import { ACADEMIC_TRACK, INDUSTRY_TRACK } from "./fixtures";
import {
  resolveTrack,
  selectTrack,
  statusLabel,
  trackHref,
  trackKey,
  trackQuery,
  useSelectedSlug,
} from "./track";

/**
 * The selected track lives in the page URL (spec 094), so it survives a
 * reload and a second tab, and the back button returns to the previous one.
 */

function Probe({ name }: { name: string }) {
  const slug = useSelectedSlug();
  return <p data-testid={name}>{slug ?? "(default)"}</p>;
}

beforeEach(() => {
  window.history.replaceState(null, "", "/");
});

afterEach(() => {
  cleanup();
  window.history.replaceState(null, "", "/");
});

test("the selected track survives a reload and a second tab", () => {
  window.history.replaceState(null, "", "/?track=second-search");
  const first = render(<Probe name="first" />);
  expect(screen.getByTestId("first").textContent).toBe("second-search");
  // A reload is a fresh mount reading the same URL.
  first.unmount();
  render(<Probe name="reloaded" />);
  expect(screen.getByTestId("reloaded").textContent).toBe("second-search");
  // A second tab is another reader of the same address: nothing is held in
  // memory that a copied URL would not carry.
  render(<Probe name="second-tab" />);
  expect(screen.getByTestId("second-tab").textContent).toBe("second-search");
});

test("switching changes the URL, and back returns to the previous track", async () => {
  render(<Probe name="probe" />);
  expect(screen.getByTestId("probe").textContent).toBe("(default)");

  act(() => {
    selectTrack("second-search");
  });
  expect(window.location.search).toBe("?track=second-search");
  expect(screen.getByTestId("probe").textContent).toBe("second-search");

  // The default track is the URL without a slug, as it was before tracks.
  act(() => {
    selectTrack(null);
  });
  expect(window.location.search).toBe("");
  expect(screen.getByTestId("probe").textContent).toBe("(default)");

  act(() => {
    window.history.back();
  });
  await waitFor(() => {
    expect(screen.getByTestId("probe").textContent).toBe("second-search");
  });
});

test("a request and a cache key name the track only when the URL does", () => {
  expect(trackQuery(null)).toEqual({});
  expect(trackQuery("second-search")).toEqual({ track: "second-search" });
  expect(trackKey("second-search", "jobs")).toEqual(["second-search", "jobs"]);
  expect(trackKey(null, "jobs")).toEqual(["", "jobs"]);
  window.history.replaceState(null, "", "/?view=x&track=second-search#top");
  expect(trackHref(null)).toBe("/?view=x#top");
});

test("the URL resolves to a track, to none while loading, or to null when unknown", () => {
  const tracks = [INDUSTRY_TRACK, ACADEMIC_TRACK];
  expect(resolveTrack(undefined, null)).toBeUndefined();
  expect(resolveTrack(tracks, null)).toBe(INDUSTRY_TRACK);
  expect(resolveTrack(tracks, "second-search")).toBe(ACADEMIC_TRACK);
  expect(resolveTrack(tracks, "no-such-track")).toBeNull();
});

test("a status reads in the track kind's own word, from the API's labels", () => {
  expect(statusLabel(ACADEMIC_TRACK, "applied")).toBe("Submitted");
  expect(statusLabel(ACADEMIC_TRACK, "tailored_cv_requested")).toBe("Preparing documents");
  expect(statusLabel(INDUSTRY_TRACK, "tailored_cv_requested")).toBe("CV requested");
  expect(statusLabel(INDUSTRY_TRACK, "applied")).toBe("Applied");
});
