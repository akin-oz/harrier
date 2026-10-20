import { expect, test } from "vitest";

import { readCapture } from "./fragment";

/**
 * The capture fragment (spec 119): `#capture?` and a query string of the six
 * fields `POST /capture/add` takes. The values are synthetic.
 */

function fragment(values: Record<string, string>): string {
  return `#capture?${new URLSearchParams(values).toString()}`;
}

test("a capture fragment yields every field", () => {
  const captured = readCapture(
    fragment({
      title: "Senior Frontend Engineer",
      company: "Example Co",
      location: "Remote, Europe",
      url: "https://jobs.example.com/postings/1",
      source: "linkedin",
      description: "Build the web app & its tests.",
    }),
  );

  expect(captured).toEqual({
    fields: {
      title: "Senior Frontend Engineer",
      company: "Example Co",
      location: "Remote, Europe",
      url: "https://jobs.example.com/postings/1",
      source: "linkedin",
      description: "Build the web app & its tests.",
    },
    linkLeftOut: false,
  });
});

test("a link that is not an http address is left out", () => {
  for (const url of ["javascript:alert(1)", "jobs/1", "ftp://files.example.com/jobs/1"]) {
    const captured = readCapture(fragment({ title: "Engineer", company: "Example Co", url }));
    expect(captured?.fields.url, url).toBe("");
    expect(captured?.linkLeftOut, url).toBe(true);
  }
  // No link at all is not a link left out.
  const absent = readCapture(fragment({ title: "Engineer", company: "Example Co" }));
  expect(absent?.fields.url).toBe("");
  expect(absent?.linkLeftOut).toBe(false);
  // An http address is kept as well as an https one.
  const plain = readCapture(fragment({ url: "http://jobs.example.com/1" }));
  expect(plain?.fields.url).toBe("http://jobs.example.com/1");
  expect(plain?.linkLeftOut).toBe(false);
});

test("the description is cut to 8000 characters", () => {
  const description = "a".repeat(4000) + "b".repeat(5000);
  const captured = readCapture(fragment({ description }));

  expect(captured?.fields.description).toBe(description.slice(0, 8000));
  expect(captured?.fields.description).toHaveLength(8000);
  // A description at the limit is kept whole.
  const exact = "c".repeat(8000);
  expect(readCapture(fragment({ description: exact }))?.fields.description).toBe(exact);
});

test("a missing source reads as manual", () => {
  expect(readCapture(fragment({ title: "Engineer" }))?.fields.source).toBe("manual");
  expect(readCapture(fragment({ source: "" }))?.fields.source).toBe("manual");
  expect(readCapture(fragment({ source: "   " }))?.fields.source).toBe("manual");
  expect(readCapture(fragment({ source: "greenhouse" }))?.fields.source).toBe("greenhouse");
});

test("a fragment that is not a capture is not read", () => {
  expect(readCapture("")).toBeNull();
  expect(readCapture("#other")).toBeNull();
  expect(readCapture("#capture")).toBeNull();
});
