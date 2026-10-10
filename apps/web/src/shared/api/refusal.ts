import type { components } from "@harrier/contract";

// What a route answers when it refuses, as the contract declares it: the
// domain's words on a 400, 404 or 409, the hold on a 503, and the field
// errors on a 422. Named from the generated types, so a body the contract
// changes fails the type check here rather than reaching the operator as the
// fallback (spec 082's pattern).
export type Refusal =
  | components["schemas"]["ErrorOut"]
  | components["schemas"]["DatabaseHeldOut"]
  | components["schemas"]["HTTPValidationError"];

/**
 * A refusal in the words the API used. A 422 names each field it refused and
 * why, so "apify_count: Input should be less than or equal to 500" reaches
 * the operator rather than a fallback that says nothing (review of PR #208).
 */
export function refusalMessage(error: Refusal, fallback: string): string {
  if (typeof error.detail === "string") return error.detail;
  const fields = (error.detail ?? []).map(
    (item) => `${String(item.loc.at(-1) ?? "request")}: ${item.msg}`,
  );
  return fields.length > 0 ? fields.join("; ") : fallback;
}
