import { vi } from "vitest";

/**
 * A controllable run event stream for component tests (spec 114). Only tests
 * import this.
 *
 * `installRunStreams()` wraps the current `fetch` stub: a request for
 * `/runs/{id}/events` gets a stream the test drives, anything else goes to
 * `streams.fallback`, which starts as the stub that was in place. A test that
 * wants to change what the other routes answer mid-test sets `fallback`
 * instead of stubbing `fetch` again, which would replace this wrapper.
 */

const EVENTS = /\/runs\/([^/]+)\/events$/;

export class RunStreamConnection {
  readonly runId: string;
  /** The path the request went to, as the browser sent it. */
  readonly path: string;
  readonly lastEventId: string | null;
  readonly authorization: string | null;
  readonly localToken: string | null;
  readonly signal: AbortSignal;
  private readonly encoder = new TextEncoder();
  private controller: ReadableStreamDefaultController<Uint8Array> | null = null;
  readonly body: ReadableStream<Uint8Array>;
  private readonly ids: () => number;

  constructor(request: Request, runId: string, ids: () => number) {
    this.runId = runId;
    this.path = new URL(request.url).pathname;
    this.lastEventId = request.headers.get("Last-Event-ID");
    this.authorization = request.headers.get("Authorization");
    this.localToken = request.headers.get("X-Harrier-Token");
    this.signal = request.signal;
    this.ids = ids;
    this.body = new ReadableStream<Uint8Array>({
      start: (controller) => {
        this.controller = controller;
      },
    });
    // A real fetch errors the body when its request is aborted.
    request.signal.addEventListener("abort", () => {
      try {
        this.controller?.error(new DOMException("aborted", "AbortError"));
      } catch {
        // already closed
      }
    });
  }

  /** Whether the request was aborted, as a closed stream would be. */
  get closed(): boolean {
    return this.signal.aborted;
  }

  /** One event, with the next id for this run. */
  emit(payload: Record<string, unknown>): void {
    this.raw(`id: ${String(this.ids())}\ndata: ${JSON.stringify(payload)}\n\n`);
  }

  raw(text: string): void {
    this.controller?.enqueue(this.encoder.encode(text));
  }

  /** The server ends the stream cleanly. */
  end(): void {
    this.controller?.close();
  }

  /** The connection drops mid-stream. */
  fail(): void {
    this.controller?.error(new TypeError("network error"));
  }
}

export class RunStreams {
  readonly connections: RunStreamConnection[] = [];
  fallback: (request: Request) => Response | Promise<Response>;
  /** The next stream request answers this status instead of a stream. */
  refuseNext: { status: number; body: unknown } | null = null;
  /** The next stream request fails as a network error does. */
  failNext = false;
  /** How many times a stream was asked for, refused or not. */
  requests = 0;
  private readonly counters = new Map<string, number>();

  constructor(fallback: (request: Request) => Response | Promise<Response>) {
    this.fallback = fallback;
  }

  get last(): RunStreamConnection {
    const found = this.connections.at(-1);
    if (found === undefined) throw new Error("no stream was opened");
    return found;
  }

  forRun(runId: string): RunStreamConnection[] {
    return this.connections.filter((connection) => connection.runId === runId);
  }

  async handle(request: Request): Promise<Response> {
    const runId = EVENTS.exec(new URL(request.url).pathname)?.[1];
    if (runId === undefined) return this.fallback(request);
    this.requests += 1;
    if (this.failNext) {
      this.failNext = false;
      throw new TypeError("Failed to fetch");
    }
    if (this.refuseNext !== null) {
      const refusal = this.refuseNext;
      this.refuseNext = null;
      return new Response(JSON.stringify(refusal.body), {
        status: refusal.status,
        headers: { "Content-Type": "application/json" },
      });
    }
    const connection = new RunStreamConnection(request, runId, () => {
      const next = (this.counters.get(runId) ?? 0) + 1;
      this.counters.set(runId, next);
      return next;
    });
    this.connections.push(connection);
    return new Response(connection.body, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });
  }
}

export function installRunStreams(): RunStreams {
  const previous = globalThis.fetch;
  const streams = new RunStreams((request) => previous(request));
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
      streams.handle(input instanceof Request ? input : new Request(String(input), init)),
    ),
  );
  return streams;
}

/**
 * Holds every timer set for exactly `delay` milliseconds until the test
 * releases it, and leaves all other timers real. The stream hook's retry wait
 * is such a timer, so a test can see that it waits, and end the wait, without
 * faking the clock `waitFor` and `userEvent` run on. Undone by
 * `vi.restoreAllMocks()`.
 */
export function holdTimers(delay: number): { readonly waiting: number; release: () => void } {
  const realSet = globalThis.setTimeout.bind(globalThis);
  const realClear = globalThis.clearTimeout.bind(globalThis);
  const held = new Map<number, () => void>();
  let next = -1;
  vi.spyOn(globalThis, "setTimeout").mockImplementation(((
    handler: () => void,
    ms?: number,
  ): number => {
    if (ms === delay) {
      const id = next;
      next -= 1;
      held.set(id, handler);
      return id;
    }
    return realSet(handler, ms);
  }) as unknown as typeof setTimeout);
  vi.spyOn(globalThis, "clearTimeout").mockImplementation((id) => {
    if (typeof id === "number" && held.delete(id)) return;
    realClear(id);
  });
  return {
    get waiting() {
      return held.size;
    },
    release() {
      const callbacks = [...held.values()];
      held.clear();
      for (const callback of callbacks) callback();
    },
  };
}
