import { expect, test } from "vitest";
import { page } from "./fixture";
import { readPage } from "./page";
import type { SiteReader } from "./sites";

const THROWS: SiteReader = {
  matches: () => true,
  read: () => {
    throw new Error("markup the reader did not expect");
  },
};

test("a site reader that throws is replaced by the generic reader", () => {
  const doc = page("Careers - Woodgrove", "<h1>Platform Engineer</h1>");

  expect(readPage(doc, "https://www.linkedin.com/jobs/view/1/", [THROWS])).toEqual({
    title: "Platform Engineer",
    company: "Woodgrove",
    location: "",
    url: "https://www.linkedin.com/jobs/view/1/",
    source: "manual",
    description: "",
  });
});

test("a page no reader can read gives only its link", () => {
  // Not a document at all, so the generic reader throws too.
  const broken = {} as Document;

  expect(readPage(broken, "https://jobs.example.com/1", [THROWS])).toEqual({
    title: "",
    company: "",
    location: "",
    url: "https://jobs.example.com/1",
    source: "manual",
    description: "",
  });
});
