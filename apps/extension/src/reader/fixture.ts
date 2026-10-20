// Synthetic job pages for the reader tests. Each is modelled on the markup
// docs/bookmarklets.md records for its board; none holds a real posting.
//
// jsdom lays nothing out, so it has no `innerText` and the readers read
// `textContent`. A fixture puts each line the page would show on its own
// line of HTML, so the line-based rules see the same lines.
export function page(title: string, body: string): Document {
  const doc = document.implementation.createHTMLDocument(title);
  doc.body.innerHTML = body;
  return doc;
}
