import { expect, test } from "vitest";
import { page } from "./fixture";
import { readPage } from "./page";

const CAREERS = "https://jobs.example.com/woodgrove/platform-engineer";

function ldJson(data: unknown): string {
  return `<script type="application/ld+json">${JSON.stringify(data)}</script>`;
}

test("a JobPosting in JSON-LD yields its fields, at the top level or in @graph", () => {
  const topLevel = page(
    "Platform Engineer",
    ldJson({
      "@context": "https://schema.org",
      "@type": "JobPosting",
      title: "Platform Engineer",
      hiringOrganization: { "@type": "Organization", name: "Woodgrove" },
      jobLocation: {
        "@type": "Place",
        address: { "@type": "PostalAddress", addressLocality: "Berlin", addressCountry: "DE" },
      },
      jobLocationType: "TELECOMMUTE",
      description: "<p>Build synthetic things.</p><ul><li>Rust</li><li>Go</li></ul>",
    }) + "<h1>Careers</h1>",
  );
  const inGraph = page(
    "Careers",
    ldJson({
      "@context": "https://schema.org",
      "@graph": [
        { "@type": "WebPage", name: "Careers" },
        {
          "@type": ["JobPosting"],
          title: "Data Engineer",
          hiringOrganization: "Woodgrove",
          jobLocation: [
            { address: { addressLocality: "Madrid", addressCountry: { name: "Spain" } } },
            { address: { addressLocality: "Valencia" } },
          ],
          description: "Move <b>synthetic</b> data.<br>Daily.",
        },
      ],
    }),
  );

  expect(readPage(topLevel, CAREERS)).toEqual({
    title: "Platform Engineer",
    company: "Woodgrove",
    location: "Berlin, DE, Remote",
    url: CAREERS,
    source: "manual",
    description: "Build synthetic things.\nRust\nGo",
  });
  expect(readPage(inGraph, CAREERS)).toEqual({
    title: "Data Engineer",
    company: "Woodgrove",
    location: "Madrid, Spain",
    url: CAREERS,
    source: "manual",
    description: "Move synthetic data.\nDaily.",
  });
});

test("a JSON-LD description escaped twice is read as text", () => {
  const doc = page(
    "Careers",
    ldJson({
      "@type": "JobPosting",
      title: "Platform Engineer",
      description: "&lt;p&gt;Build synthetic things.&lt;/p&gt;&lt;p&gt;Remote.&lt;/p&gt;",
    }),
  );

  expect(readPage(doc, CAREERS).description).toBe("Build synthetic things.\nRemote.");
});

test("a page with malformed JSON-LD falls back to its h1", () => {
  const doc = page(
    "Careers - Woodgrove",
    `<script type="application/ld+json">{ "@type": "JobPosting", "title": </script>
     ${ldJson({ "@type": "Organization", name: "Woodgrove" })}
     <h1>Platform Engineer</h1>`,
  );

  expect(readPage(doc, CAREERS)).toEqual({
    title: "Platform Engineer",
    company: "Woodgrove",
    location: "",
    url: CAREERS,
    source: "manual",
    description: "",
  });
});

test("a page with no h1 takes its title and company from the page title", () => {
  const doc = page("Platform Engineer - Woodgrove | Jobs", "<p>Apply now</p>");

  const fields = readPage(doc, CAREERS);

  expect(fields.title).toBe("Platform Engineer");
  expect(fields.company).toBe("Woodgrove");
});
