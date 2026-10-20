// @vitest-environment node
// esbuild refuses to run under jsdom, whose TextEncoder is not Node's, so the
// page here is a JSDOM of its own.
import { build } from "esbuild";
import { JSDOM } from "jsdom";
import { expect, test } from "vitest";
import { READER } from "../../scripts/options";

test("the built reader script ends with the page's fields", async () => {
  const bundle = await build({ ...READER, write: false });
  const code = bundle.outputFiles[0]?.text ?? "";
  const tab = new JSDOM("<title>Careers - Woodgrove</title><h1>Platform Engineer</h1>", {
    url: "https://jobs.example.com/woodgrove/1",
    runScripts: "outside-only",
  });

  // What executeScript hands back: the value of the script's last statement.
  const result = tab.window.eval(code);

  expect(result).toEqual({
    title: "Platform Engineer",
    company: "Woodgrove",
    location: "",
    url: "https://jobs.example.com/woodgrove/1",
    source: "manual",
    description: "",
  });
});
