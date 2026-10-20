/** Where the web app's address is kept, in `chrome.storage.local`. */
export const ADDRESS_KEY = "webAppAddress";

export const ADDRESS_FORMS =
  "Enter an https:// address, or http://localhost or http://127.0.0.1 with an optional port.";

/** The web app's origin when `input` is one the extension accepts, else
 * null: an `https://` origin, or `http://localhost` or `http://127.0.0.1`
 * with an optional port, which is spec 119's page in local mode. A path,
 * query, fragment or user name is refused, so the capture address is always
 * the origin and the fragment. */
export function webAppOrigin(input: string): string | null {
  let url: URL;
  try {
    url = new URL(input.trim());
  } catch {
    return null;
  }
  const local =
    url.protocol === "http:" && (url.hostname === "localhost" || url.hostname === "127.0.0.1");
  if (url.protocol !== "https:" && !local) return null;
  if (url.username || url.password || url.search || url.hash || url.pathname !== "/") return null;
  return url.origin;
}

/** The stored web app origin, or null when none is set. */
export async function storedOrigin(store: Store): Promise<string | null> {
  const saved = (await store.get(ADDRESS_KEY))[ADDRESS_KEY];
  return typeof saved === "string" ? webAppOrigin(saved) : null;
}

/** The part of `chrome.storage.local` the extension uses. */
export interface Store {
  get(key: string): Promise<Record<string, unknown>>;
  set(items: Record<string, unknown>): Promise<void>;
}
