import { ADDRESS_FORMS, ADDRESS_KEY, storedOrigin, webAppOrigin, type Store } from "../address";

const ASK = "Set the web app address, then click the Harrier icon again.";
const SAVED = "Saved. Click the Harrier icon on a job page to capture it.";

/** The options page's one setting: the web app address (spec 161). */
export async function mountOptions(doc: Document, store: Store): Promise<void> {
  const form = doc.createElement("form");
  const label = doc.createElement("label");
  label.textContent = "Web app address";
  const input = doc.createElement("input");
  input.type = "url";
  input.name = "address";
  input.placeholder = "https://harrier.example.com";
  label.append(input);
  const save = doc.createElement("button");
  save.type = "submit";
  save.textContent = "Save";
  const message = doc.createElement("p");
  message.setAttribute("role", "status");
  form.append(label, save, message);
  doc.body.append(form);

  const saved = await storedOrigin(store);
  if (saved === null) message.textContent = ASK;
  else input.value = saved;

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const origin = webAppOrigin(input.value);
    if (origin === null) {
      message.textContent = ADDRESS_FORMS;
      return;
    }
    void store.set({ [ADDRESS_KEY]: origin }).then(() => {
      input.value = origin;
      message.textContent = SAVED;
    });
  });
}
