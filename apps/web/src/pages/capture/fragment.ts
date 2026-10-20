import type { components } from "@harrier/contract";

type CaptureIn = components["schemas"]["CaptureIn"];

/** Every field `POST /capture/add` takes, each one filled. Typed from the
 * contract, so a field the server renames or drops fails the type check. */
export type CaptureFields = { [Name in keyof CaptureIn]-?: string };

export interface Captured {
  readonly fields: CaptureFields;
  /** The fragment carried a link that was not an http(s) address. */
  readonly linkLeftOut: boolean;
}

// The capture page's address is the app's own origin and this fragment
// (spec 119). A fragment is never sent to the server, so the fields stay
// out of the host's request logs, as a query string would not.
export const CAPTURE_FRAGMENT = "#capture?";

// The bookmarklets' limit. The server cuts again, to 4000 (spec 010).
const DESCRIPTION_LIMIT = 8000;

/** The fields a capture fragment carries, or null for any other fragment. */
export function readCapture(hash: string): Captured | null {
  if (!hash.startsWith(CAPTURE_FRAGMENT)) return null;
  const params = new URLSearchParams(hash.slice(CAPTURE_FRAGMENT.length));
  const read = (name: keyof CaptureIn): string => params.get(name) ?? "";
  const link = read("url").trim();
  const kept = webAddress(link);
  return {
    fields: {
      title: read("title"),
      company: read("company"),
      location: read("location"),
      url: kept,
      source: read("source").trim() || "manual",
      // By code point, so a cut never splits a character in two.
      description: Array.from(read("description")).slice(0, DESCRIPTION_LIMIT).join(""),
    },
    linkLeftOut: link !== "" && kept === "",
  };
}

// Only an absolute http(s) address is kept, the rule spec 142 applies to
// LinkedIn links: a page that crafts the fragment cannot plant a
// `javascript:` link in the tracker.
function webAddress(value: string): string {
  try {
    const { protocol } = new URL(value);
    return protocol === "http:" || protocol === "https:" ? value : "";
  } catch {
    return "";
  }
}

/** Takes the fragment out of the address, keeping the path and the track in
 * the query string, so a reload opens the tracker rather than a second
 * capture. */
export function forgetCaptureFragment(): void {
  const { pathname, search } = globalThis.location;
  globalThis.history.replaceState(globalThis.history.state, "", pathname + search);
}
