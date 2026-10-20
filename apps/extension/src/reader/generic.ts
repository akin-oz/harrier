import type { CaptureFields } from "../fields";
import { pageTitle, splitPageTitle, text } from "./dom";

/** The reader for every page no site reader claims (spec 161): a
 * schema.org `JobPosting` in the page's JSON-LD, else the first `h1` or the
 * page title. A posting with no title or company takes them from the `h1`
 * and the page title. The link is the page address. */
export function readGeneric(doc: Document, url: URL): CaptureFields {
  const split = splitPageTitle(pageTitle(doc));
  const heading = text(doc.querySelector("h1")) || split.title;
  const posting = jobPosting(doc);
  if (posting) {
    return {
      ...posting,
      title: posting.title || heading,
      company: posting.company || split.company,
      url: url.href,
      source: "manual",
    };
  }
  return {
    title: heading,
    company: split.company,
    location: "",
    url: url.href,
    source: "manual",
    description: "",
  };
}

type Posting = Pick<CaptureFields, "title" | "company" | "location" | "description">;
type Json = Record<string, unknown>;

/** The first `JobPosting` in any JSON-LD block, at the top level or inside
 * `@graph`. A block that does not parse is skipped. */
function jobPosting(doc: Document): Posting | null {
  for (const script of doc.querySelectorAll('script[type="application/ld+json"]')) {
    let data: unknown;
    try {
      data = JSON.parse(script.textContent);
    } catch {
      continue;
    }
    const found = findPosting(data);
    if (found) return postingFields(found);
  }
  return null;
}

function findPosting(data: unknown): Json | null {
  for (const item of Array.isArray(data) ? data : [data]) {
    const node = object(item);
    if (!node) continue;
    if (isPosting(node)) return node;
    const graph = node["@graph"];
    if (Array.isArray(graph)) {
      const inGraph = findPosting(graph);
      if (inGraph) return inGraph;
    }
  }
  return null;
}

function isPosting(node: Json): boolean {
  const type = node["@type"];
  return type === "JobPosting" || (Array.isArray(type) && type.includes("JobPosting"));
}

function postingFields(posting: Json): Posting {
  const organization = posting.hiringOrganization;
  const place = object(firstOf(posting.jobLocation));
  const address = object(place?.address);
  const country = address?.addressCountry;
  const remote = firstOf(posting.jobLocationType) === "TELECOMMUTE" ? "Remote" : "";
  return {
    title: string(posting.title),
    company:
      typeof organization === "string" ? organization.trim() : string(object(organization)?.name),
    location: [
      string(address?.addressLocality),
      string(object(country)?.name) || string(country),
      remote,
    ]
      .filter(Boolean)
      .join(", "),
    description: htmlToText(string(posting.description)),
  };
}

function object(value: unknown): Json | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Json)
    : null;
}

function firstOf(value: unknown): unknown {
  return Array.isArray(value) ? (value as unknown[])[0] : value;
}

function string(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

// Elements that start a new line when the page lays them out.
const BLOCKS = new Set([
  "P",
  "DIV",
  "LI",
  "UL",
  "OL",
  "H1",
  "H2",
  "H3",
  "H4",
  "H5",
  "H6",
  "TR",
  "SECTION",
  "ARTICLE",
  "BLOCKQUOTE",
  "PRE",
  "TABLE",
]);

/** A JSON-LD description's HTML as plain text, one line per block. Some
 * boards escape the markup twice, so text that still holds tags after one
 * pass is read again. */
export function htmlToText(html: string): string {
  if (!html) return "";
  try {
    let plain = blockText(parse(html));
    if (/<\/?[a-z][^>]*>/i.test(plain)) plain = blockText(parse(plain));
    return tidy(plain);
  } catch {
    // A page whose policy refuses the parser still gives the text.
    return tidy(html.replace(/<[^>]*>/g, " "));
  }
}

// An inert document: its scripts never run and its images never load.
function parse(html: string): Node {
  return new DOMParser().parseFromString(html, "text/html").body;
}

function blockText(node: Node): string {
  if (node.nodeType === Node.TEXT_NODE) return (node.textContent ?? "").replace(/\s+/g, " ");
  if (node.nodeType !== Node.ELEMENT_NODE) return "";
  const name = (node as Element).tagName;
  if (name === "BR") return "\n";
  const inner = Array.from(node.childNodes, blockText).join("");
  return BLOCKS.has(name) ? `\n${inner}\n` : inner;
}

function tidy(plain: string): string {
  return plain
    .split("\n")
    .map((line) => line.replace(/[ \t\u00a0]+/g, " ").trim())
    .filter(Boolean)
    .join("\n");
}
