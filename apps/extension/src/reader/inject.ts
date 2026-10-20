// The script the toolbar click injects into the active tab. The build
// (scripts/build.ts) wraps it so its last statement is these fields, which
// is what `chrome.scripting.executeScript` hands back.
import { readPage } from "./page";

export const fields = readPage(document, location.href);
