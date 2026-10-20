import { expect, test } from "vitest";
import { page } from "./fixture";
import { readPage } from "./page";

const SEARCH = "https://hiring.cafe/?searchState=%7B%7D";

test("a hiring.cafe page with its job dialog open yields the viewjob link", () => {
  const doc = page(
    "HiringCafe",
    `<h1>Search jobs</h1>
     <div role="dialog" aria-modal="true">
       <a href="/viewjob/abc123def">Open</a>
       <h2>Site Reliability Engineer</h2>
       <span>@ Tailspin Toys</span>
       <div>
         <svg viewBox="0 0 24 24"><path d="M19.5 10.5c0 7.142-7.5 11.25-7.5 11.25"></path></svg>
         <span>Remote, EU</span>
       </div>
       <article>Keep synthetic systems up.</article>
     </div>`,
  );

  expect(readPage(doc, SEARCH)).toEqual({
    title: "Site Reliability Engineer",
    company: "Tailspin Toys",
    location: "Remote, EU",
    url: "https://hiring.cafe/viewjob/abc123def",
    source: "hiringcafe_manual",
    description: "Keep synthetic systems up.",
  });
});

test("a hiring.cafe page with no job dialog is read by the generic reader", () => {
  const doc = page("HiringCafe", "<h1>Search jobs</h1><div>Cards</div>");

  expect(readPage(doc, SEARCH)).toEqual({
    title: "Search jobs",
    company: "",
    location: "",
    url: SEARCH,
    source: "manual",
    description: "",
  });
});
