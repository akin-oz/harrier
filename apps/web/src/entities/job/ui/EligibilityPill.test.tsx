import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";

import type { Eligibility } from "../eligibility";
import { EligibilityPill } from "./EligibilityPill";

afterEach(cleanup);

function eligibility(overrides: Partial<Eligibility>): Eligibility {
  return {
    verdict: "unknown",
    routes: [],
    origin: "rule",
    evidence_url: "",
    excerpt: "",
    field: "",
    checked_at: "",
    version: "",
    description_sha256: "",
    disagrees: false,
    ...overrides,
  };
}

test("the pill names the verdict and routes", () => {
  const { container } = render(
    <EligibilityPill eligibility={eligibility({ verdict: "possible", routes: ["contractor"] })} />,
  );
  expect(container.textContent).toBe("Türkiye possible · contractor or B2B");
  expect(container.querySelector(".eligibility-pill--possible")).not.toBeNull();
});

test("an unknown verdict shows no pill", () => {
  const { container } = render(<EligibilityPill eligibility={eligibility({ origin: "none" })} />);
  expect(container.textContent).toBe("");
});

test("the pill marks your verdict and a disagreement", () => {
  const { container } = render(
    <EligibilityPill
      eligibility={eligibility({ verdict: "confirmed", origin: "candidate", disagrees: true })}
    />,
  );
  // In the words, so the marks are in the accessible name, not in colour.
  expect(container.textContent).toContain("Türkiye confirmed");
  expect(container.textContent).toContain("your verdict");
  expect(container.textContent).toContain("the rules differ");
});
