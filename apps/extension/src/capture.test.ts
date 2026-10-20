import { expect, test } from "vitest";
import { capturePage, captureSelection, DEFAULT_TITLE, READER_FILE, UNREADABLE } from "./capture";
import { fakeBrowser, fragmentFields } from "./fake";

const ORIGIN = "https://harrier.example.com";
const TAB = { id: 7, index: 2, url: "https://jobs.example.com/woodgrove/1" };

test("the capture address carries the fields in its fragment", async () => {
  const read = {
    title: "C++ & Rust / Go engineer",
    company: "Woodgrove",
    location: "Berlin, DE",
    url: "https://jobs.example.com/woodgrove/1?ref=feed",
    source: "manual",
    description: "x".repeat(9000),
  };
  const { browser, calls } = fakeBrowser({
    address: ORIGIN,
    inject: () => Promise.resolve([{ result: read }]),
  });

  await capturePage(browser, TAB);

  expect(calls.injected).toEqual([{ target: { tabId: 7 }, files: [READER_FILE] }]);
  expect(calls.created).toHaveLength(1);
  const [opened] = calls.created;
  expect(opened?.index).toBe(3);
  expect(opened?.openerTabId).toBe(7);
  const address = new URL(opened?.url ?? "");
  expect(address.origin).toBe(ORIGIN);
  expect(address.pathname).toBe("/");
  expect(address.search).toBe("");
  expect(address.hash.startsWith("#capture?")).toBe(true);
  expect(fragmentFields(opened?.url ?? "")).toEqual({
    ...read,
    description: "x".repeat(8000),
  });
});

test("a selection becomes the description", async () => {
  const { browser, calls } = fakeBrowser({
    address: ORIGIN,
    inject: () => Promise.resolve([{ result: "  First line\nSecond line \n" }]),
  });

  await captureSelection(
    browser,
    { selectionText: "First line Second line" },
    { id: 7, index: 2, url: "https://news.example.com/issue/12" },
  );

  expect(calls.created).toHaveLength(1);
  expect(fragmentFields(calls.created[0]?.url ?? "")).toEqual({
    title: "",
    company: "",
    location: "",
    url: "https://news.example.com/issue/12",
    source: "manual",
    description: "First line\nSecond line",
  });
});

test("a selection the tab will not give is read from the menu", async () => {
  // A PDF in Chrome's viewer: the injection is refused, and the tab's address
  // is not a web page.
  const { browser, calls } = fakeBrowser({
    address: ORIGIN,
    inject: () => Promise.reject(new Error("Cannot access contents of the page")),
  });

  await captureSelection(
    browser,
    { selectionText: "Quoted posting text", frameId: 0 },
    { id: 7, index: 2, url: "chrome-extension://viewer/index.html" },
  );

  expect(calls.injected[0]?.target).toEqual({ tabId: 7, frameIds: [0] });
  const fields = fragmentFields(calls.created[0]?.url ?? "");
  expect(fields.description).toBe("Quoted posting text");
  expect(fields.url).toBe("");
});

test("a whitespace-only selection opens the capture page with only the link", async () => {
  const { browser, calls } = fakeBrowser({
    address: ORIGIN,
    inject: () => Promise.resolve([{ result: " \n " }]),
  });

  await captureSelection(browser, { selectionText: " " }, TAB);

  expect(fragmentFields(calls.created[0]?.url ?? "")).toEqual({
    title: "",
    company: "",
    location: "",
    url: TAB.url,
    source: "manual",
    description: "",
  });
});

test("with no address set, the click opens the options page and no capture tab", async () => {
  const { browser, calls } = fakeBrowser({});

  await capturePage(browser, TAB);
  await captureSelection(browser, { selectionText: "Some text" }, TAB);

  expect(calls.optionsOpened).toBe(2);
  expect(calls.injected).toEqual([]);
  expect(calls.created).toEqual([]);
});

test("a refused injection sets the badge and opens no tab", async () => {
  const { browser, calls } = fakeBrowser({
    address: ORIGIN,
    inject: () => Promise.reject(new Error("Cannot access a chrome:// URL")),
  });

  await capturePage(browser, { id: 7, index: 0, url: "chrome://settings/" });

  expect(calls.badges.at(-1)).toEqual({ tabId: 7, text: "!" });
  expect(calls.titles.at(-1)).toEqual({ tabId: 7, title: UNREADABLE });
  expect(UNREADABLE).toBe("Harrier can't read this page");
  expect(calls.created).toEqual([]);
});

test("the next click clears the badge", async () => {
  const { browser, calls } = fakeBrowser({
    address: ORIGIN,
    inject: () => Promise.resolve([{ result: { title: "Platform Engineer" } }]),
  });

  await capturePage(browser, TAB);

  expect(calls.badges).toEqual([{ tabId: 7, text: "" }]);
  expect(calls.titles).toEqual([{ tabId: 7, title: DEFAULT_TITLE }]);
  expect(calls.created).toHaveLength(1);
});

test("a reader result that is not fields opens the capture page with only the link", async () => {
  const { browser, calls } = fakeBrowser({
    address: ORIGIN,
    inject: () => Promise.resolve([{}]),
  });

  await capturePage(browser, TAB);

  expect(fragmentFields(calls.created[0]?.url ?? "")).toEqual({
    title: "",
    company: "",
    location: "",
    url: TAB.url,
    source: "manual",
    description: "",
  });
});
