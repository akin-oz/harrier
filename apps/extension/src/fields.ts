import type { components } from "@harrier/contract";

type CaptureIn = components["schemas"]["CaptureIn"];

/** The six fields `POST /capture/add` takes, each one filled. Typed from the
 * contract, so a field the server renames or drops fails the type check. */
export type CaptureFields = { [Name in keyof CaptureIn]-?: string };

/** Every field empty but the link. */
export function onlyTheLink(url: string): CaptureFields {
  return { title: "", company: "", location: "", url, source: "manual", description: "" };
}

// Spec 119's capture page reads this fragment. A fragment is never sent to
// the server, so the fields stay out of the host's request logs.
const CAPTURE_FRAGMENT = "#capture?";

// The bookmarklets' limit, which the capture page applies too (spec 119).
const DESCRIPTION_LIMIT = 8000;

/** The capture page's address for these fields: the web app's origin, then
 * the fields URL-encoded in the fragment, never in a query string. */
export function captureAddress(origin: string, fields: CaptureFields): string {
  const params = new URLSearchParams({
    title: fields.title,
    company: fields.company,
    location: fields.location,
    url: fields.url,
    source: fields.source,
    // By code point, so a cut never splits a character in two.
    description: Array.from(fields.description).slice(0, DESCRIPTION_LIMIT).join(""),
  });
  return `${origin}/${CAPTURE_FRAGMENT}${params.toString()}`;
}

/** The address when it is an http(s) page, otherwise empty. */
export function webAddress(value: string | undefined): string {
  if (value === undefined) return "";
  try {
    const { protocol } = new URL(value);
    return protocol === "http:" || protocol === "https:" ? value : "";
  } catch {
    return "";
  }
}
