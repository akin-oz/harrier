// The extension's service worker: the toolbar click and the context menu.
import { capturePage, captureSelection, MENU_ID, MENU_TITLE } from "./capture";

chrome.runtime.onInstalled.addListener(() => {
  // Shown only when text is selected.
  chrome.contextMenus.create({ id: MENU_ID, title: MENU_TITLE, contexts: ["selection"] });
});

chrome.action.onClicked.addListener((tab) => {
  void capturePage(chrome, tab);
});

chrome.contextMenus.onClicked.addListener((click, tab) => {
  if (click.menuItemId === MENU_ID) void captureSelection(chrome, click, tab);
});
