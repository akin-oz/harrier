import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";

import { JobTable } from "./JobTable";
import type { Job } from "./types";

// vitest globals are off, so RTL never auto-cleans: without this the DOM
// accumulates rows across tests and row-order assertions read stale renders.
afterEach(cleanup);

function makeJob(overrides: Partial<Job>): Job {
  return {
    id: 1,
    company: "Acme",
    title: "Senior Frontend Engineer",
    location: "Remote, Europe",
    url: "https://boards.example.com/acme/1",
    source: "greenhouse",
    added_at: "2026-08-01",
    fit_score: "80",
    status: "prospect",
    applied_date: "",
    last_contact: "",
    next_action: "review and decide whether to apply",
    outreach_status: "",
    last_outreach_at: "",
    next_outreach_action: "",
    best_contact_name: "",
    best_contact_linkedin: "",
    contacts_found: "",
    outreach_priority: "",
    rejection_reason: "",
    notes: "",
    score: "80",
    archetype: "",
    source_label: "",
    external_key: "",
    signals: "",
    remote_filter: "",
    manual_added: "",
    created_at: "",
    updated_at: "",
    ...overrides,
  };
}

test("renders one row per job with a titled link", () => {
  render(
    <JobTable
      jobs={[makeJob({}), makeJob({ id: 2, company: "Beta", title: "Product Engineer", url: "" })]}
      emptyMessage="No jobs match."
    />,
  );
  expect(screen.getByRole("link", { name: "Senior Frontend Engineer" })).toBeDefined();
  expect(screen.getByText("Beta")).toBeDefined();
  expect(screen.getAllByRole("row")).toHaveLength(3);
});

test("empty list renders the message the page supplied", () => {
  render(<JobTable jobs={[]} emptyMessage="No jobs yet. Run discovery to find some." />);
  expect(screen.getByText("No jobs yet. Run discovery to find some.")).toBeDefined();
});

function companyOrder(): (string | undefined)[] {
  // Reads the company element rather than the whole cell: company and title
  // now share one column, so the cell's text is both plus the meta line. The
  // ordering being asserted is unchanged.
  return screen
    .getAllByRole("row")
    .slice(1)
    .map((row) => row.querySelector(".job-table__company")?.textContent);
}

test("rows are ordered by score, highest first", () => {
  // Answers "which of these is best" without a click; the API returns
  // insertion order (spec 026).
  render(
    <JobTable
      jobs={[
        makeJob({ id: 1, company: "Low", score: "60" }),
        makeJob({ id: 2, company: "High", score: "110" }),
        makeJob({ id: 3, company: "Mid", score: "85" }),
      ]}
      emptyMessage="none"
    />,
  );
  expect(companyOrder()).toEqual(["High", "Mid", "Low"]);
});

test("open rows outrank closed ones however they scored", () => {
  // Score alone fills the first screen with closed rows once most of the
  // tracker is rejected, which is the steady state of a real search.
  render(
    <JobTable
      jobs={[
        makeJob({ id: 1, company: "ClosedTop", score: "120", status: "rejected" }),
        makeJob({ id: 2, company: "OpenLow", score: "58", status: "prospect" }),
        makeJob({ id: 3, company: "ClosedMid", score: "100", status: "rejected" }),
        makeJob({ id: 4, company: "OpenHigh", score: "90", status: "applied" }),
      ]}
      emptyMessage="none"
    />,
  );
  expect(companyOrder()).toEqual(["OpenHigh", "OpenLow", "ClosedTop", "ClosedMid"]);
});

test("a blank score renders as unknown rather than zero", () => {
  render(<JobTable jobs={[makeJob({ score: "", fit_score: "" })]} emptyMessage="none" />);
  expect(screen.getByLabelText("no score")).toBeDefined();
});

test("status carries a text label, not colour alone", () => {
  render(<JobTable jobs={[makeJob({ status: "tailored_cv_requested" })]} emptyMessage="none" />);
  expect(screen.getByText("CV requested")).toBeDefined();
});
