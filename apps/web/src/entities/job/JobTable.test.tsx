import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";

import { JobTable } from "./JobTable";
import type { Job } from "./types";

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
    manual_reject: "",
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
    />,
  );
  expect(screen.getByRole("link", { name: "Senior Frontend Engineer" })).toBeDefined();
  expect(screen.getByText("Beta")).toBeDefined();
  expect(screen.getAllByRole("row")).toHaveLength(3);
});

test("empty list renders the empty message", () => {
  render(<JobTable jobs={[]} />);
  expect(screen.getByText("No jobs match.")).toBeDefined();
});
