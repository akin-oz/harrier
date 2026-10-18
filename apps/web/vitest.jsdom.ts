import { builtinEnvironments, type Environment } from "vitest/environments";

// jsdom with Node's own AbortController and AbortSignal.
//
// jsdom installs its AbortSignal over Node's. Node's `Request`, which the API
// client builds every call on, accepts only Node's AbortSignal, and since
// Node 26 it refuses jsdom's: "Expected signal to be an instance of
// AbortSignal". CI runs Node 22, which still accepted it. The app passes a
// signal only to `fetch`, `Request` and its own listeners, never to a jsdom
// API, so Node's pair serves every test on any Node.
const environment: Environment = {
  name: "jsdom-node-abort",
  transformMode: "web",
  async setup(global: typeof globalThis, options) {
    const { AbortController, AbortSignal } = global;
    const jsdom = await builtinEnvironments.jsdom.setup(global, options);
    Object.assign(global, { AbortController, AbortSignal });
    return jsdom;
  },
};

export default environment;
