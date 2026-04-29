// Loaded once when DevTools opens on a tab. Registers the api-medic
// panel; all UI lives in panel.html / panel.tsx, so this file does
// nothing else.
chrome.devtools.panels.create("api-medic", "", "panel.html", () => {
  // Panel created.
});
