import { expect, test } from "vitest";
import { page } from "./fixture";
import { readPage } from "./page";

test("a Wellfound split pane is read from the job panel, not the search bar", () => {
  const doc = page(
    "Startup Jobs | Wellfound",
    `<header><h1>Search startup jobs</h1></header>
     <aside><a href="https://wellfound.com/company/promoted-co">Promoted Co</a></aside>
     <div data-test="JobListingSlideIn">
       <img alt="Avatar for Contoso Robotics" src="data:," />
       <h1>Backend Engineer</h1>
       <ul><li>Full Time | Remote (Europe)</li></ul>
       <div id="job-description">Write services for a synthetic robot fleet.</div>
     </div>`,
  );

  expect(
    readPage(doc, "https://wellfound.com/jobs?job_listing_slug=1000001-backend-engineer"),
  ).toEqual({
    title: "Backend Engineer",
    company: "Contoso Robotics",
    location: "Remote (Europe)",
    url: "https://wellfound.com/jobs?job_listing_slug=1000001-backend-engineer",
    source: "wellfound_manual",
    description: "Write services for a synthetic robot fleet.",
  });
});
