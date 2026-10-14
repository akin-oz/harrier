import { afterEach, expect, test, vi } from "vitest";

import { EventStreamParser, StreamRefused, readRunEvents } from "./stream";
import type { StreamMessage } from "./stream";

afterEach(() => {
  vi.unstubAllGlobals();
});

function parse(...chunks: string[]): StreamMessage[] {
  const parser = new EventStreamParser();
  return chunks.flatMap((chunk) => parser.push(chunk));
}

test("the event-stream parser follows the line rules", () => {
  const event = (id: string, data: string): StreamMessage => ({ lastEventId: id, data });

  // Lines end in CRLF, LF or CR, and mean the same in each.
  for (const end of ["\r\n", "\n", "\r"]) {
    expect(parse(`id: 1${end}data: one${end}${end}`), JSON.stringify(end)).toEqual([
      event("1", "one"),
    ]);
  }
  // A comment is ignored, whatever it says, and dispatches nothing alone.
  expect(parse(": token expired\n\n")).toEqual([]);
  expect(parse(": hello\nid: 2\ndata: two\n\n")).toEqual([event("2", "two")]);
  // `data` lines are joined with a newline; one leading space is dropped.
  expect(parse("id: 3\ndata: a\ndata:  b\ndata\n\n")).toEqual([event("3", "a\n b\n")]);
  // `event` and `retry` are read and ignored; an id with a NUL is ignored.
  expect(parse("event: x\nretry: 10\nid: 4\ndata: four\n\n")).toEqual([event("4", "four")]);
  expect(parse("id: 5\ndata: five\n\nid: a\0b\ndata: six\n\n")).toEqual([
    event("5", "five"),
    event("5", "six"),
  ]);
  // The id persists until the next one; a blank line with no data dispatches
  // nothing.
  expect(parse("id: 7\n\ndata: seven\n\n")).toEqual([event("7", "seven")]);
  // Several events in one chunk.
  expect(parse("id: 8\ndata: a\n\nid: 9\ndata: b\n\n")).toEqual([event("8", "a"), event("9", "b")]);
  // A chunk ends inside a line, inside a field name, and between a CR and
  // its LF.
  expect(parse("id: 1\nda", "ta: spl", "it\n", "\n")).toEqual([event("1", "split")]);
  expect(parse("id: 1\r", "\ndata: x\r", "\n\r", "\n")).toEqual([event("1", "x")]);
  // The LF of a split CRLF is not a blank line that would end the event early.
  expect(parse("data: a\r", "\ndata: b\r\n\r\n")).toEqual([event("", "a\nb")]);
  // An event with no closing blank line is not dispatched.
  expect(parse("id: 1\ndata: unfinished\n")).toEqual([]);
});

/** A stream of these byte chunks, as a server's response body. */
function body(chunks: Uint8Array[]): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk);
      controller.close();
    },
  });
}

function stubStream(chunks: Uint8Array[], status = 200): { sent: Request[] } {
  const sent: Request[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      sent.push(input instanceof Request ? input : new Request(String(input)));
      return Promise.resolve(
        status === 200
          ? new Response(body(chunks), { status })
          : new Response(JSON.stringify({ detail: "no" }), {
              status,
              headers: { "Content-Type": "application/json" },
            }),
      );
    }),
  );
  return { sent };
}

async function collect(lastEventId = 0): Promise<StreamMessage[]> {
  const messages: StreamMessage[] = [];
  for await (const message of readRunEvents("r1", {
    lastEventId,
    signal: new AbortController().signal,
  })) {
    messages.push(message);
  }
  return messages;
}

test("a chunk boundary inside a multi-byte character loses nothing", async () => {
  const bytes = new TextEncoder().encode('id: 1\ndata: {"line":"café → ok"}\n\n');
  // Split inside the two-byte "e acute" and inside the three-byte arrow.
  const acute = bytes.indexOf(0xc3) + 1;
  const arrow = bytes.indexOf(0xe2) + 1;
  stubStream([bytes.slice(0, acute), bytes.slice(acute, arrow), bytes.slice(arrow)]);

  expect(await collect()).toEqual([{ lastEventId: "1", data: '{"line":"café → ok"}' }]);
});

test("the stream is asked for with the last id it holds", async () => {
  const { sent } = stubStream([]);
  await collect(0);
  await collect(12);

  expect(sent[0]?.headers.get("Accept")).toBe("text/event-stream");
  expect(sent[0]?.headers.has("Last-Event-ID")).toBe(false);
  expect(sent[1]?.headers.get("Last-Event-ID")).toBe("12");
  expect(new URL(sent[1]?.url ?? "").pathname).toBe("/api/runs/r1/events");
});

test("a stream refused with a status says which", async () => {
  stubStream([], 404);
  await expect(collect()).rejects.toMatchObject({ name: "StreamRefused", status: 404 });
  await expect(collect()).rejects.toBeInstanceOf(StreamRefused);
});
