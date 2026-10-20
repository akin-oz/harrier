import { expect, test } from "vitest";
import { page } from "./fixture";
import { readPage } from "./page";

const JOB =
  "https://www.welcometothejungle.com/en/companies/fabrikam-group/jobs/data-engineer_paris";

test("a Welcome to the Jungle page yields its fields", () => {
  const doc = page(
    "Data Engineer - Fabrikam | Welcome to the Jungle",
    `<nav><a href="/fr/entreprises"><span>Français</span></a></nav>
     <div>
       <div data-testid="job-metadata-block">
         <a href="/en/companies/fabrikam-group"><span>Fabrikam</span></a>
       </div>
       <h2>Data Engineer</h2>
     </div>
     <div><svg alt="Location"></svg><span>Paris</span></div>
     <div><svg alt="Remote"></svg><span>Fully remote</span></div>
     <div data-testid="job-section-description">Move synthetic data between synthetic places.</div>`,
  );

  expect(readPage(doc, JOB)).toEqual({
    title: "Data Engineer",
    company: "Fabrikam",
    location: "Paris, Fully remote",
    url: JOB,
    source: "wttj_manual",
    description: "Move synthetic data between synthetic places.",
  });
});

test("a Welcome to the Jungle page with no metadata block falls back to its title and slug", () => {
  const doc = page("Data Engineer | Welcome to the Jungle", "<main><p>Loading</p></main>");

  const fields = readPage(doc, JOB);

  expect(fields.title).toBe("Data Engineer");
  expect(fields.company).toBe("fabrikam group");
  expect(fields.source).toBe("wttj_manual");
});
