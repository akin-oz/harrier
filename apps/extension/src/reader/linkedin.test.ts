import { expect, test } from "vitest";
import { page } from "./fixture";
import { readPage } from "./page";

const JOB = "https://www.linkedin.com/jobs/view/4000000001/?refId=abc123&trackingId=def456";

test("a LinkedIn job page yields its fields", () => {
  const doc = page(
    "Senior Platform Engineer | Northwind Labs | LinkedIn",
    `<h1>Senior Platform Engineer</h1>
     <div><a href="https://www.linkedin.com/company/northwind-labs/">Northwind Labs</a></div>
     <div>Northwind Labs · Lisbon, Portugal (Remote) · 2 days ago</div>
     <div data-testid="expandable-text-box">Run the platform of a synthetic product.</div>`,
  );

  expect(readPage(doc, JOB)).toEqual({
    title: "Senior Platform Engineer",
    company: "Northwind Labs",
    location: "Lisbon, Portugal (Remote)",
    url: "https://www.linkedin.com/jobs/view/4000000001/",
    source: "linkedin_manual",
    description: "Run the platform of a synthetic product.",
  });
});

test("a LinkedIn page with a two-part title takes the company from its link", () => {
  // A /jobs/view/ page: "Title - Company | LinkedIn". The site's own links
  // come first and are not companies.
  const doc = page(
    "Senior Platform Engineer - Northwind Labs | LinkedIn",
    `<a href="https://www.linkedin.com/company/linkedin/">LinkedIn</a>
     <a href="https://www.linkedin.com/company/signin/">Sign in</a>
     <a href="https://www.linkedin.com/company/northwind-labs/">Northwind Labs</a>
     <div>Northwind Labs • Porto, Portugal • 1 week ago</div>`,
  );

  const fields = readPage(doc, JOB);

  expect(fields.title).toBe("Senior Platform Engineer");
  expect(fields.company).toBe("Northwind Labs");
  expect(fields.location).toBe("Porto, Portugal");
});

test("a LinkedIn search page links to the selected job", () => {
  // The split pane names the selected job only in its query string.
  const doc = page(
    "Senior Platform Engineer | Northwind Labs | LinkedIn",
    "<h1>Senior Platform Engineer</h1>",
  );

  const fields = readPage(
    doc,
    "https://www.linkedin.com/jobs/search/?currentJobId=4000000002&keywords=platform&refId=abc123&trackingId=def456",
  );

  expect(fields.url).toBe("https://www.linkedin.com/jobs/view/4000000002/");
});

test("a LinkedIn title's unread count is not part of the job title", () => {
  const doc = page("(3) Senior Platform Engineer | Northwind Labs | LinkedIn", "<h1>Jobs</h1>");

  expect(readPage(doc, JOB).title).toBe("Senior Platform Engineer");
});
