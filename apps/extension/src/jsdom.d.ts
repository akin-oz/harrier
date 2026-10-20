// The one jsdom API the reader bundle test uses (src/reader/inject.test.ts).
// @types/jsdom is left out on purpose: it brings @types/node, which pnpm
// then resolves as vite's optional peer for the web app too, and the web
// app's timers would type as Node's.
declare module "jsdom" {
  export class JSDOM {
    constructor(html: string, options: { url: string; runScripts: "outside-only" });
    readonly window: { eval(code: string): unknown };
  }
}
