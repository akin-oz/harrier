// Small readers of the page the site readers and the generic reader share,
// ported from the bookmarklets (docs/bookmarklets.md at 72066ab).

/** An element's text as the owner sees it, trimmed. `innerText` keeps the
 * line breaks the page lays out; an element with none, such as an `svg`
 * child, gives its text content. */
export function text(node: Element | null | undefined): string {
  if (!node) return "";
  const shown = (node as Partial<HTMLElement>).innerText;
  return (shown ?? node.textContent).trim();
}

/** The text of the first element any selector matches, in selector order,
 * skipping an element with no text. */
export function first(root: ParentNode, ...selectors: string[]): string {
  for (const selector of selectors) {
    const found = text(root.querySelector(selector));
    if (found) return found;
  }
  return "";
}

/** The bookmarklet's "Title - Company" split of a page title: the part
 * before the first `|`, cut at its last ` - `. */
export function splitPageTitle(pageTitle: string): { title: string; company: string } {
  const head = (pageTitle.split("|")[0] ?? "").trim();
  const dash = head.lastIndexOf(" - ");
  if (dash > 0) {
    return { title: head.slice(0, dash).trim(), company: head.slice(dash + 3).trim() };
  }
  return { title: head, company: "" };
}

/** The bookmarklet's title cleaning: a ` - https://...` suffix some boards
 * put in the title goes, and so does a ` - Company` suffix naming the
 * company already read. */
export function cleanTitle(title: string, company: string): string {
  let cleaned = title;
  const link = cleaned.lastIndexOf(" - http");
  if (link > 0) cleaned = cleaned.slice(0, link).trim();
  if (company) {
    const dash = cleaned.lastIndexOf(" - ");
    if (
      dash > 0 &&
      cleaned
        .slice(dash + 3)
        .trim()
        .toLowerCase() === company.toLowerCase()
    ) {
      cleaned = cleaned.slice(0, dash).trim();
    }
  }
  return cleaned;
}

/** The host is this one or a subdomain of it. */
export function onHost(url: URL, host: string): boolean {
  return url.hostname === host || url.hostname.endsWith(`.${host}`);
}
