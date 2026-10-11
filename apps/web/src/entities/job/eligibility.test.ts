import { expect, expectTypeOf, test } from "vitest";

import {
  ORIGIN_LABEL,
  ROUTE_LABEL,
  ROUTE_ORDER,
  VERDICT_LABEL,
  VERDICT_ORDER,
  eligibilitySummary,
  isVerdict,
  isWebLink,
} from "./eligibility";
import type { EligibilityOrigin, EligibilityRoute, EligibilityVerdict } from "./eligibility";

/**
 * One table of words for spec 122's values (spec 123). The tables are typed
 * as a `Record` over the contract's unions, so a value the contract adds is
 * a type error here; these hold that the orders name every value once.
 */

test("words cover the contract", () => {
  expectTypeOf(VERDICT_LABEL).toEqualTypeOf<Record<EligibilityVerdict, string>>();
  expectTypeOf(ROUTE_LABEL).toEqualTypeOf<Record<EligibilityRoute, string>>();
  expectTypeOf(ORIGIN_LABEL).toEqualTypeOf<Record<EligibilityOrigin, string>>();
  expect([...VERDICT_ORDER].sort()).toEqual(Object.keys(VERDICT_LABEL).sort());
  expect([...ROUTE_ORDER].sort()).toEqual(Object.keys(ROUTE_LABEL).sort());
  expect(VERDICT_ORDER).toEqual(["confirmed", "possible", "unknown", "excluded"]);
});

test("a summary is the verdict, then its routes", () => {
  expect(eligibilitySummary({ verdict: "possible", routes: ["contractor", "eor"] })).toBe(
    "Türkiye possible · contractor or B2B · employer of record",
  );
  expect(eligibilitySummary({ verdict: "excluded", routes: [] })).toBe("Türkiye excluded");
});

test("only http and https are links", () => {
  expect(isWebLink("https://jobs.example.com/acme/1")).toBe(true);
  expect(isWebLink("http://jobs.example.com/acme/1")).toBe(true);
  expect(isWebLink("javascript:alert(1)")).toBe(false);
  expect(isWebLink("ftp://jobs.example.com")).toBe(false);
  expect(isVerdict("possible")).toBe(true);
  expect(isVerdict("maybe")).toBe(false);
});
