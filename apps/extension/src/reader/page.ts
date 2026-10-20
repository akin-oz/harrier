import { onlyTheLink, type CaptureFields } from "../fields";
import { cleanTitle } from "./dom";
import { readGeneric } from "./generic";
import { SITE_READERS, type SiteReader } from "./sites";

/** What the page the owner clicked on says about its job (spec 161).
 *
 * The site reader for the page's address runs first. When it finds no
 * title, or throws on markup it did not expect, the generic reader runs.
 * When that throws too, only the link is sent. */
export function readPage(
  doc: Document,
  href: string,
  sites: readonly SiteReader[] = SITE_READERS,
): CaptureFields {
  const url = new URL(href);
  const site = sites.find((reader) => reader.matches(url));
  if (site) {
    try {
      const fields = site.read(doc, url);
      if (fields.title) return tidy(fields);
    } catch {
      // The generic reader below.
    }
  }
  try {
    return tidy(readGeneric(doc, url));
  } catch {
    return onlyTheLink(href);
  }
}

function tidy(fields: CaptureFields): CaptureFields {
  const company = fields.company.trim();
  return {
    title: cleanTitle(fields.title.trim(), company),
    company,
    location: fields.location.trim(),
    url: fields.url,
    source: fields.source,
    description: fields.description.trim(),
  };
}
