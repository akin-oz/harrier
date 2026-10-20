// Writes the unpacked extension to apps/extension/dist (docs/extension.md).
import { build } from "esbuild";
import { MODULES, READER, ROOT } from "./options";

const dist = `${ROOT}dist`;

await build({ ...READER, outfile: `${dist}/reader.js` });
await build({ ...MODULES, outdir: dist });
// The manifest and the options page go in as they are.
await build({
  entryPoints: [`${ROOT}src/manifest.json`, `${ROOT}src/options.html`],
  loader: { ".json": "copy", ".html": "copy" },
  outdir: dist,
});
console.log(`Built the extension in ${dist}`);
