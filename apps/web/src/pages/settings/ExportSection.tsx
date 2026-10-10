import { useMutation } from "@tanstack/react-query";

import { api } from "../../shared/api/client";
import { refusalMessage } from "../../shared/api/refusal";
import { trackQuery, useSelectedSlug } from "../../shared/track";

type Download = "jobs.csv" | "contacts.csv";

async function fetchCsv(name: Download, slug: string | null): Promise<Blob> {
  // The token travels in the header the client adds, never in the URL, so it
  // reaches neither browser history nor an access log (spec 096).
  const result =
    name === "jobs.csv"
      ? await api.GET("/ops/export/jobs.csv", {
          params: { query: trackQuery(slug) },
          parseAs: "blob",
        })
      : await api.GET("/ops/export/contacts.csv", { parseAs: "blob" });
  if (result.error !== undefined)
    throw new Error(refusalMessage(result.error, `${name} was refused`));
  // No body is the token refusal, the only declared outcome without one
  // (spec 035). Not `instanceof Blob`: the body's Blob can come from another
  // realm than the page's, and on Node 22 under jsdom it does.
  if (result.data === undefined) throw new Error("refused: the local API token was not accepted");
  return result.data;
}

/** Hand the file to the browser's own download, and keep nothing. */
function save(blob: Blob, name: Download): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  URL.revokeObjectURL(url);
}

/**
 * `harrier export` as two downloads (spec 096, replacing spec 050's export
 * run): the same columns, written by the same function, saved by the
 * browser. Nothing is written on the server. Contacts belong to the person,
 * so they are offered on the default track only, as on the command line.
 */
export function ExportSection() {
  const slug = useSelectedSlug();
  const download = useMutation({
    mutationFn: async (name: Download) => {
      save(await fetchCsv(name, slug), name);
      return name;
    },
  });

  return (
    <div className="settings-export">
      <div className="settings-backups__toolbar">
        <button
          type="button"
          className="settings-button"
          disabled={download.isPending}
          onClick={() => {
            download.mutate("jobs.csv");
          }}
        >
          Download jobs.csv
        </button>
        <button
          type="button"
          className="settings-button"
          disabled={download.isPending || slug !== null}
          aria-describedby={slug !== null ? "export-contacts-note" : undefined}
          onClick={() => {
            download.mutate("contacts.csv");
          }}
        >
          Download contacts.csv
        </button>
      </div>
      <p id="export-contacts-note" className="settings-muted">
        {slug === null
          ? "The jobs of this track and your contacts, in the columns harrier export writes. Formula characters at the start of a cell are kept as text."
          : "The jobs of this track. Contacts are yours rather than a track's, so they download on the default track."}
      </p>
      {download.error !== null && (
        <p role="alert" className="settings-error">
          {download.error.message}
        </p>
      )}
      <p role="status" className="settings-muted">
        {download.isSuccess ? `${download.data} downloaded.` : ""}
      </p>
    </div>
  );
}
