import type { Browser } from "./capture";

type Injection = Parameters<Browser["scripting"]["executeScript"]>[0];

/** A fake of the Chrome APIs the extension calls, recording each call. */
export function fakeBrowser(options: {
  address?: string;
  inject?: (injection: Injection) => Promise<{ result?: unknown }[]>;
}) {
  const calls = {
    injected: [] as Injection[],
    created: [] as { url: string; index: number; openerTabId: number }[],
    badges: [] as { tabId: number; text: string }[],
    titles: [] as { tabId: number; title: string }[],
    optionsOpened: 0,
  };
  const browser: Browser = {
    storage: {
      local: {
        get: (key) =>
          Promise.resolve(options.address === undefined ? {} : { [key]: options.address }),
        set: () => Promise.resolve(),
      },
    },
    scripting: {
      executeScript: (injection) => {
        calls.injected.push(injection);
        return options.inject ? options.inject(injection) : Promise.resolve([]);
      },
    },
    tabs: {
      create: (properties) => {
        calls.created.push(properties);
        return Promise.resolve({});
      },
    },
    runtime: {
      openOptionsPage: () => {
        calls.optionsOpened += 1;
        return Promise.resolve();
      },
    },
    action: {
      setBadgeText: (details) => {
        calls.badges.push(details);
        return Promise.resolve();
      },
      setTitle: (details) => {
        calls.titles.push(details);
        return Promise.resolve();
      },
    },
  };
  return { browser, calls };
}

/** The fields a capture address carries in its fragment. */
export function fragmentFields(address: string): Record<string, string> {
  const { hash } = new URL(address);
  return Object.fromEntries(new URLSearchParams(hash.slice("#capture?".length)));
}
