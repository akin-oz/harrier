import { api } from "./client";

/**
 * Reading a run's event stream with `fetch` (spec 114).
 *
 * The browser's built-in event-source reader cannot send an `Authorization`
 * header, so the hosted API's bearer token cannot reach it (spec 104, Open
 * decision 4). The stream is read
 * as a `GET` on the generated client with `parseAs: "stream"`, so the token
 * middleware applies to it like every other call, and the body is parsed here
 * by the rules of the WHATWG event-stream format. The server sends only `id`
 * and `data` (`format_sse` in `harrier_api/runs.py`) and, hosted, a comment
 * at token expiry (spec 132), so `event` and `retry` are read and ignored.
 */

/** One dispatched event: its `id` and its `data`, lines joined by a newline. */
export interface StreamMessage {
  readonly lastEventId: string;
  readonly data: string;
}

/**
 * The event-stream line rules as an incremental parser: text in, whole events
 * out. A chunk may end inside a line, and a CRLF may be split between two
 * chunks; both are carried to the next `push`.
 */
export class EventStreamParser {
  private tail = "";
  // The last chunk ended on a CR, so an LF opening the next one is its pair.
  private skipLineFeed = false;
  private data: string[] = [];
  private lastEventId = "";

  push(text: string): StreamMessage[] {
    const messages: StreamMessage[] = [];
    if (text === "") return messages;
    const input = this.skipLineFeed && text.startsWith("\n") ? text.slice(1) : text;
    this.skipLineFeed = false;
    const buffer = this.tail + input;
    let start = 0;
    for (let i = 0; i < buffer.length; i += 1) {
      const char = buffer[i];
      if (char !== "\n" && char !== "\r") continue;
      const line = buffer.slice(start, i);
      if (char === "\r") {
        if (buffer[i + 1] === "\n") i += 1;
        else if (i + 1 === buffer.length) this.skipLineFeed = true;
      }
      start = i + 1;
      const message = this.line(line);
      if (message !== null) messages.push(message);
    }
    this.tail = buffer.slice(start);
    return messages;
  }

  private line(line: string): StreamMessage | null {
    if (line === "") {
      if (this.data.length === 0) return null;
      const message = { lastEventId: this.lastEventId, data: this.data.join("\n") };
      this.data = [];
      return message;
    }
    // A line starting with a colon is a comment. Its field name is empty, so
    // it matches no field below and is ignored without a branch of its own.
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "data") this.data.push(value);
    else if (field === "id" && !value.includes("\0")) this.lastEventId = value;
    return null;
  }
}

/** The stream answered with a status other than 200. */
export class StreamRefused extends Error {
  readonly status: number;

  constructor(status: number) {
    super(`the event stream answered ${String(status)}`);
    this.name = "StreamRefused";
    this.status = status;
  }
}

/**
 * Every event of one run, from `lastEventId` on, until the server ends the
 * stream. A clean end is a return; a refusal throws `StreamRefused`, and a
 * network failure throws what `fetch` threw. Aborting `signal` ends it.
 */
export async function* readRunEvents(
  runId: string,
  options: { lastEventId: number; signal: AbortSignal },
): AsyncGenerator<StreamMessage> {
  const headers: Record<string, string> = { Accept: "text/event-stream" };
  if (options.lastEventId > 0) headers["Last-Event-ID"] = String(options.lastEventId);
  const { data, response } = await api.GET("/runs/{run_id}/events", {
    params: { path: { run_id: runId } },
    parseAs: "stream",
    headers,
    signal: options.signal,
  });
  if (response.status !== 200) throw new StreamRefused(response.status);
  if (data === undefined || data === null) return;
  const reader = data.getReader();
  const decoder = new TextDecoder();
  const parser = new EventStreamParser();
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) return;
      yield* parser.push(decoder.decode(value, { stream: true }));
    }
  } finally {
    await reader.cancel().catch(() => undefined);
  }
}
