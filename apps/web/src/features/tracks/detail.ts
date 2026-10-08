// A refusal from the tracks routes, in the domain's words. A 404 or a 409
// carries the sentence `harrier tracks` prints; a 422 is a list of field
// errors that only a malformed request can produce, so it reads as the
// fallback rather than as a schema dump.
export function detailOf(error: unknown, fallback: string): string {
  if (typeof error === "object" && error !== null && "detail" in error) {
    if (typeof error.detail === "string") return error.detail;
  }
  return fallback;
}
