import type { components } from "@harrier/contract";

import type { Job } from "./types";

// Whether a posting can engage someone based in Türkiye, as the API serves it
// (spec 122). The verdict is geography only; the routes are engagement only.
export type Eligibility = Job["eligibility"];
export type EligibilityEntry = components["schemas"]["EligibilityEntryOut"];
export type EligibilityVerdict = Eligibility["verdict"];
export type EligibilityRoute = Eligibility["routes"][number];
export type EligibilityOrigin = Eligibility["origin"];

// One table of words, typed as a `Record` over the contract's unions, so a
// value the contract adds is a compile error here until it has words
// (spec 123).
export const VERDICT_LABEL: Record<EligibilityVerdict, string> = {
  confirmed: "Türkiye confirmed",
  possible: "Türkiye possible",
  unknown: "Türkiye unknown",
  excluded: "Türkiye excluded",
};

export const ROUTE_LABEL: Record<EligibilityRoute, string> = {
  employee: "employee",
  contractor: "contractor or B2B",
  eor: "employer of record",
  relocation: "relocation",
};

export const ORIGIN_LABEL: Record<EligibilityOrigin, string> = {
  rule: "the rules",
  candidate: "your verdict",
  none: "not assessed",
};

// The order every list of verdicts reads in.
export const VERDICT_ORDER: readonly EligibilityVerdict[] = [
  "confirmed",
  "possible",
  "unknown",
  "excluded",
];

export const ROUTE_ORDER: readonly EligibilityRoute[] = [
  "employee",
  "contractor",
  "eor",
  "relocation",
];

// "Türkiye possible · contractor or B2B": the verdict, then its routes.
export function eligibilitySummary(eligibility: Pick<Eligibility, "verdict" | "routes">): string {
  const routes = eligibility.routes.map((route) => ROUTE_LABEL[route]);
  return [VERDICT_LABEL[eligibility.verdict], ...routes].join(" · ");
}

// An evidence URL becomes a link only when it is http or https, whatever the
// store holds (spec 123).
export function isWebLink(value: string): boolean {
  return /^https?:\/\//i.test(value);
}
