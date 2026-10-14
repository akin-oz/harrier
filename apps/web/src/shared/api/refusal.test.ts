import { expect, test } from "vitest";

import { refusalMessage } from "./refusal";

test("a credential refusal reads as its field and problem", () => {
  expect(
    refusalMessage(
      {
        detail: [
          { field: "key", problem: "is empty" },
          { field: "model", problem: "is longer than 4096 bytes" },
        ],
      },
      "fallback",
    ),
  ).toBe("key: is empty; model: is longer than 4096 bytes");
});

test("the other refusals keep reading as they did", () => {
  expect(refusalMessage({ detail: "a sentence" }, "fallback")).toBe("a sentence");
  expect(
    refusalMessage({ detail: [{ loc: ["body", "limit"], msg: "too large", type: "x" }] }, "f"),
  ).toBe("limit: too large");
  expect(refusalMessage({ detail: [] }, "fallback")).toBe("fallback");
  expect(refusalMessage({ reason: "keys_unavailable", detail: "Try again shortly." }, "f")).toBe(
    "Try again shortly.",
  );
});
