import { afterEach, expect, test, vi } from "vitest";
import { ADDRESS_FORMS, type Store } from "../address";
import { mountOptions } from "./form";

function fakeStore(saved: Record<string, unknown> = {}) {
  const writes: Record<string, unknown>[] = [];
  const store: Store = {
    get: () => Promise.resolve(saved),
    set: (items) => {
      writes.push(items);
      return Promise.resolve();
    },
  };
  return { store, writes };
}

async function submit(address: string): Promise<void> {
  const input = document.querySelector("input");
  if (!input) throw new Error("no address field");
  input.value = address;
  document.querySelector("form")?.requestSubmit();
  await Promise.resolve();
}

function message(): string {
  return document.querySelector('[role="status"]')?.textContent ?? "";
}

afterEach(() => {
  document.body.replaceChildren();
});

test("the address must be https or localhost", async () => {
  const { store, writes } = fakeStore();
  await mountOptions(document, store);

  await submit("http://example.com");
  expect(message()).toBe(ADDRESS_FORMS);
  expect(writes).toEqual([]);

  await submit("https://example.com");
  await vi.waitFor(() => {
    expect(writes).toEqual([{ webAppAddress: "https://example.com" }]);
  });

  await submit("http://localhost:5173");
  await vi.waitFor(() => {
    expect(writes.at(-1)).toEqual({ webAppAddress: "http://localhost:5173" });
  });

  await submit("http://127.0.0.1:8000/");
  await vi.waitFor(() => {
    expect(writes.at(-1)).toEqual({ webAppAddress: "http://127.0.0.1:8000" });
  });
});

test("an address with a path, query or fragment is refused", async () => {
  const { store, writes } = fakeStore();
  await mountOptions(document, store);

  for (const address of [
    "https://example.com/app",
    "https://example.com/?a=1",
    "https://example.com/#x",
    "ftp://example.com",
    "localhost:5173",
  ]) {
    await submit(address);
    expect(message()).toBe(ADDRESS_FORMS);
  }
  expect(writes).toEqual([]);
});

test("with no address saved the page asks for one", async () => {
  const { store } = fakeStore();

  await mountOptions(document, store);

  expect(message()).toBe("Set the web app address, then click the Harrier icon again.");
  expect(document.querySelector("input")?.value).toBe("");
});

test("a saved address is shown in the field", async () => {
  const { store } = fakeStore({ webAppAddress: "https://harrier.example.com" });

  await mountOptions(document, store);

  expect(document.querySelector("input")?.value).toBe("https://harrier.example.com");
  expect(message()).toBe("");
});
