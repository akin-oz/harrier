import type { CaptureFields } from "../fields";
import { first, onHost, splitPageTitle, text } from "./dom";

/** A reader for one job board. Each one ports that board's bookmarklet
 * rules from docs/bookmarklets.md at 72066ab, where the selector reasoning
 * is recorded. A reader that finds no title hands the page to the generic
 * reader (spec 161). */
export interface SiteReader {
  readonly matches: (url: URL) => boolean;
  readonly read: (doc: Document, url: URL) => CaptureFields;
}

// LinkedIn is a React app with hashed class names, so the page title and
// the body text are read rather than CSS classes. A `/jobs/view/` page has a
// two-part title, "Title | LinkedIn", so the company link is the fallback.
const linkedin: SiteReader = {
  matches: (url) => onHost(url, "linkedin.com") && url.pathname.startsWith("/jobs"),
  read(doc, url) {
    const parts = doc.title.split(" | ");
    const last = (parts[parts.length - 1] ?? "").trim();
    let company = parts.length >= 3 && last === "LinkedIn" ? (parts[1] ?? "").trim() : "";
    if (!company) company = linkedinCompanyLink(doc);
    return {
      title: (parts[0] ?? "").trim(),
      company,
      location: linkedinLocation(text(doc.body), company),
      // Without the query string, which carries only tracking parameters.
      url: url.origin + url.pathname,
      source: "linkedin_manual",
      description: first(
        doc,
        '[data-testid="expandable-text-box"]',
        ".jobs-description__content .jobs-description-content__text",
        "#job-details",
        ".jobs-description",
      ),
    };
  },
};

// The site's own links, which also point at company pages.
const NOT_A_COMPANY = /^(LinkedIn|Sign in|Join now|Post a job|Post jobs for free)$/i;

function linkedinCompanyLink(doc: Document): string {
  for (const link of doc.querySelectorAll('a[href*="/company/"]')) {
    const name = text(link);
    if (name.length >= 2 && name.length <= 80 && !NOT_A_COMPANY.test(name)) return name;
  }
  return "";
}

// "Company · Location", with either middle dot; else "Location · 3 days ago".
function linkedinLocation(body: string, company: string): string {
  if (company) {
    const escaped = company.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const after = new RegExp(`${escaped}\\s*[·•]\\s*([^\n\r·•]+)`).exec(body);
    if (after?.[1]) return after[1].trim();
  }
  const posted = /([A-Za-z][^·•\n\r]{2,50}?)\s*[·•]\s*\d+\s*(?:hour|day|week|month|year)/i.exec(
    body,
  );
  return posted?.[1]?.trim() ?? "";
}

// The `/jobs?job_listing_slug=...` page is a split pane: its first `h1` is
// the search bar and its sidebar links are promoted companies. The selected
// job's panel, `JobListingSlideIn`, is the only part read when it is there.
const wellfound: SiteReader = {
  matches: (url) => onHost(url, "wellfound.com"),
  read(doc, url) {
    const panel = doc.querySelector('[data-test="JobListingSlideIn"]');
    const scope: ParentNode = panel ?? doc;
    const heading = scope.querySelector("h1");
    let title = text(heading);
    const avatar = panel?.querySelector<HTMLImageElement>('img[alt^="Avatar for "]');
    let company = avatar ? avatar.alt.replace(/^Avatar for /, "").trim() : "";
    if (!company && panel) company = first(panel, 'a[href*="wellfound.com/company/"]');
    // The pill row after the title: "Full Time | Remote (Europe)".
    const pills = panel ? heading?.nextElementSibling : null;
    const row = pills?.tagName === "UL" ? text(pills) : "";
    const pieces = row.split("|");
    const location = (pieces.length > 1 ? pieces.slice(1).join("|") : (pieces[0] ?? "")).trim();
    if (!title || !company) {
      // "Title at Company | Wellfound"
      const fallback = (doc.title.split("|")[0] ?? "").split(" at ");
      title ||= (fallback[0] ?? "").trim();
      company ||= (fallback[1] ?? "").trim();
    }
    return {
      title,
      company,
      location,
      url: url.href,
      source: "wellfound_manual",
      description: first(scope, "#job-description"),
    };
  },
};

// The title is the `h2` after the metadata block. The company link is
// looked for inside that block, so the nav's locale switcher is not read.
const wttj: SiteReader = {
  matches: (url) => onHost(url, "welcometothejungle.com"),
  read(doc, url) {
    const block = '[data-testid="job-metadata-block"]';
    let title = first(doc, `${block} ~ h2`);
    let company = first(
      doc,
      `${block} a[href*="/en/companies/"] span`,
      `${block} a[href*="/fr/entreprises/"] span`,
    );
    if (!title || !company) {
      // "Title - COMPANY | Welcome to the Jungle"
      const fallback = splitPageTitle(doc.title);
      title ||= fallback.title;
      company ||= fallback.company;
    }
    if (!company) {
      const slug = /\/companies\/([^/]+)\//.exec(url.pathname);
      if (slug?.[1]) company = slug[1].replace(/-/g, " ");
    }
    return {
      title,
      company,
      location: [chip(doc, "Location"), chip(doc, "Remote")].filter(Boolean).join(", "),
      url: url.href,
      source: "wttj_manual",
      description: first(doc, '[data-testid="job-section-description"]'),
    };
  },
};

// A chip is an icon, `svg[alt=...]`, beside a `span` holding the text.
function chip(doc: Document, alt: string): string {
  const icon = doc.querySelector(`svg[alt="${alt}"]`);
  return text(icon?.parentElement?.querySelector("span"));
}

// hiring.cafe shows a job in a modal dialog opened from a search card. Its
// class names are utility strings that change; the dialog role, the
// "@ Company" span, the map-pin path and the `/viewjob/` link do not.
const hiringcafe: SiteReader = {
  matches: (url) => onHost(url, "hiring.cafe"),
  read(doc, url) {
    const dialog = doc.querySelector('[role="dialog"][aria-modal="true"]');
    if (!dialog) {
      return { title: "", company: "", location: "", url: url.href, source: "", description: "" };
    }
    let company = "";
    for (const span of dialog.querySelectorAll("span")) {
      const said = text(span);
      if (/^@\s/.test(said)) {
        company = said.replace(/^@\s+/, "").trim();
        break;
      }
    }
    let location = "";
    for (const icon of dialog.querySelectorAll("svg")) {
      if (icon.innerHTML.includes("10.5c0 7.142")) {
        location = text(icon.parentElement?.querySelector("span"));
        break;
      }
    }
    const view = dialog.querySelector('a[href^="/viewjob/"]')?.getAttribute("href");
    return {
      title: text(dialog.querySelector("h2")),
      company,
      location,
      url: view ? new URL(view, "https://hiring.cafe").href : url.href,
      source: "hiringcafe_manual",
      description: text(dialog.querySelector("article")),
    };
  },
};

/** The site readers, tried in order against the page's address. */
export const SITE_READERS: readonly SiteReader[] = [linkedin, wellfound, wttj, hiringcafe];
