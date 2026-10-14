import type { components } from "@harrier/contract";

// What a route answers when it refuses, as the contract declares it: the
// domain's words on a 400, 404 or 409, the hold on a 503, the field errors
// on a 422, the hosted API's token refusal on a 401, 403 or 503 (spec 104),
// the hosted API's refusal to start a run on a 501 (spec 132), the credential
// store's field and problem on a 422 (spec 116) and Telegram's reason on a 400
// or 502 (spec 109).
// Named from the generated types, so a body the contract changes fails the
// type check here rather than reaching the operator as the fallback (spec
// 082's pattern).
export type Refusal =
  | components["schemas"]["ErrorOut"]
  | components["schemas"]["DatabaseHeldOut"]
  | components["schemas"]["HTTPValidationError"]
  | components["schemas"]["AuthRefusalOut"]
  | components["schemas"]["CredentialRefusalOut"]
  | components["schemas"]["TelegramRefusalOut"];

/**
 * A refusal in the words the API used. A 422 names each field it refused and
 * why, so "apify_count: Input should be less than or equal to 500" reaches
 * the operator rather than a fallback that says nothing (review of PR #208).
 * The credential store's 422 names its field and problem the same way, and
 * never the value it refused (spec 116).
 */
export function refusalMessage(error: Refusal, fallback: string): string {
  if (typeof error.detail === "string") return error.detail;
  const fields = (error.detail ?? []).map((item) =>
    "loc" in item
      ? `${String(item.loc.at(-1) ?? "request")}: ${item.msg}`
      : `${item.field}: ${item.problem}`,
  );
  return fields.length > 0 ? fields.join("; ") : fallback;
}
