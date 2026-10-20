// The one Node API the build reads (scripts/options.ts). @types/node is left
// out on purpose: pnpm would then resolve it as vite's optional peer for the
// web app too, and the web app's timers would type as Node's.
declare module "node:url" {
  export function fileURLToPath(url: string | URL): string;
}
