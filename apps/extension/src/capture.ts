import { storedOrigin, type Store } from "./address";
import { captureAddress, onlyTheLink, webAddress, type CaptureFields } from "./fields";

/** The built reader script (src/reader/inject.ts). */
export const READER_FILE = "reader.js";
export const MENU_ID = "add-selection";
export const MENU_TITLE = "Add selection to Harrier";
/** The toolbar tooltip, as the manifest sets it. */
export const DEFAULT_TITLE = "Add this page to Harrier";
export const UNREADABLE = "Harrier can't read this page";

type Target = { tabId: number; frameIds?: number[] };

/** The Chrome APIs the extension calls. The background passes `chrome`
 * itself; tests pass a fake. */
export interface Browser {
  readonly storage: { readonly local: Store };
  readonly scripting: {
    executeScript(
      injection: { target: Target; files: string[] } | { target: Target; func: () => string },
    ): Promise<{ result?: unknown }[]>;
  };
  readonly tabs: {
    create(properties: { url: string; index: number; openerTabId: number }): Promise<unknown>;
  };
  readonly runtime: { openOptionsPage(): Promise<void> };
  readonly action: {
    setBadgeText(details: { tabId: number; text: string }): Promise<void>;
    setTitle(details: { tabId: number; title: string }): Promise<void>;
  };
}

export interface Tab {
  readonly id?: number;
  readonly index: number;
  readonly url?: string;
}

export interface MenuClick {
  readonly selectionText?: string;
  readonly frameId?: number;
}

/** The toolbar click: read the page and open the capture page beside it. */
export async function capturePage(browser: Browser, tab: Tab): Promise<void> {
  const tabId = tab.id;
  if (tabId === undefined) return;
  // The badge a refused page left clears on the next click.
  await browser.action.setBadgeText({ tabId, text: "" });
  await browser.action.setTitle({ tabId, title: DEFAULT_TITLE });
  const origin = await storedOrigin(browser.storage.local);
  if (origin === null) {
    await browser.runtime.openOptionsPage();
    return;
  }
  let result: unknown;
  try {
    const [injected] = await browser.scripting.executeScript({
      target: { tabId },
      files: [READER_FILE],
    });
    result = injected?.result;
  } catch {
    // chrome:// pages, the Web Store, another extension's page.
    await browser.action.setBadgeText({ tabId, text: "!" });
    await browser.action.setTitle({ tabId, title: UNREADABLE });
    return;
  }
  await openCapture(browser, { ...tab, id: tabId }, origin, asFields(result, webAddress(tab.url)));
}

/** The context menu's "Add selection to Harrier": the selection becomes
 * the description, and the owner types the rest on the capture page. */
export async function captureSelection(
  browser: Browser,
  click: MenuClick,
  tab: Tab | undefined,
): Promise<void> {
  const tabId = tab?.id;
  if (tab === undefined || tabId === undefined) return;
  const origin = await storedOrigin(browser.storage.local);
  if (origin === null) {
    await browser.runtime.openOptionsPage();
    return;
  }
  let selection = "";
  try {
    const target: Target =
      click.frameId === undefined ? { tabId } : { tabId, frameIds: [click.frameId] };
    const [injected] = await browser.scripting.executeScript({ target, func: readSelection });
    if (typeof injected?.result === "string") selection = injected.result;
  } catch {
    // A PDF viewer or a frame the extension cannot reach: the menu's copy.
  }
  if (!selection.trim()) selection = click.selectionText ?? "";
  const fields = { ...onlyTheLink(webAddress(tab.url)), description: selection.trim() };
  await openCapture(browser, { ...tab, id: tabId }, origin, fields);
}

// Runs in the tab. `getSelection()` keeps the line breaks the menu's
// `selectionText` collapses.
function readSelection(): string {
  return globalThis.getSelection()?.toString() ?? "";
}

async function openCapture(
  browser: Browser,
  tab: Tab & { id: number },
  origin: string,
  fields: CaptureFields,
): Promise<void> {
  await browser.tabs.create({
    url: captureAddress(origin, fields),
    index: tab.index + 1,
    openerTabId: tab.id,
  });
}

/** The reader's result as fields. It crossed from the page, so each field is
 * checked; anything else gives only the link. */
function asFields(result: unknown, link: string): CaptureFields {
  if (typeof result !== "object" || result === null) return onlyTheLink(link);
  const read = (name: keyof CaptureFields): string => {
    const value = (result as Record<string, unknown>)[name];
    return typeof value === "string" ? value : "";
  };
  return {
    title: read("title"),
    company: read("company"),
    location: read("location"),
    url: webAddress(read("url")) || link,
    source: read("source") || "manual",
    description: read("description"),
  };
}
