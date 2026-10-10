import { useMutation } from "@tanstack/react-query";
import { useState } from "react";

import type { components } from "@harrier/contract";

import type { RunOut, useRunStream } from "../../features/runs/useRunStream";
import { api } from "../../shared/api/client";
import { TOKEN_REFUSED, refusalMessage } from "./RunOutput";
import { SOURCE_LABEL, isSource } from "./sources";
import type { SourceName } from "./sources";

type Fields = components["schemas"]["Body_runDiscover"];
type Uploads = Partial<Record<"dataset_file" | "wellfound_file" | "wttj_file", File>>;

const UPLOADS = [
  { field: "dataset_file", label: "Apify dataset export" },
  { field: "wellfound_file", label: "Wellfound export" },
  { field: "wttj_file", label: "Welcome to the Jungle export" },
] as const;

/**
 * Discovery with its options (`harrier discover`, spec 011). The run
 * streams on the Runs panel beside it, which shares this stream, so the two
 * cannot start discoveries over each other. The three export files are
 * uploaded rather than named by path: the server cannot see this machine's
 * files, and no path from the browser reaches the command (spec 095).
 */
export function DiscoverySection({ stream }: { stream: ReturnType<typeof useRunStream> }) {
  const [dryRun, setDryRun] = useState(false);
  const [notify, setNotify] = useState(true);
  const [shadow, setShadow] = useState(false);
  const [onlySource, setOnlySource] = useState<SourceName | null>(null);
  const [apifyCount, setApifyCount] = useState("");
  const [uploads, setUploads] = useState<Uploads>({});

  const start = useMutation({
    mutationFn: async (): Promise<RunOut> => {
      const fields: Fields = {
        dry_run: dryRun,
        notify,
        shadow,
        only_source: onlySource,
        apify_count: apifyCount.trim() === "" ? null : Number(apifyCount),
      };
      const { data, error } = await api.POST("/ops/discover", {
        body: fields,
        // Multipart, with each chosen file as the upload its field names.
        bodySerializer: (body: Fields | undefined) => {
          const form = new FormData();
          for (const [key, value] of Object.entries(body ?? {})) {
            if (value !== null) form.append(key, String(value));
          }
          for (const [key, file] of Object.entries(uploads)) {
            form.append(key, file);
          }
          return form;
        },
      });
      if (error !== undefined) throw new Error(refusalMessage(error, "discovery was refused"));
      if (data === undefined) throw new Error(TOKEN_REFUSED);
      return data;
    },
    onSuccess: (data) => {
      stream.begin(data);
    },
  });

  return (
    <section className="ops-section" aria-labelledby="ops-discover">
      <h3 id="ops-discover" className="ops-section__heading">
        Discovery with options
      </h3>
      <div className="ops-actions">
        <label className="ops-check">
          <input
            type="checkbox"
            checked={dryRun}
            onChange={(event) => {
              setDryRun(event.target.checked);
            }}
          />
          <span>Dry run, write nothing (still fetches every source)</span>
        </label>
        <label className="ops-check">
          <input
            type="checkbox"
            checked={shadow}
            onChange={(event) => {
              setShadow(event.target.checked);
            }}
          />
          <span>Shadow: a dry run with no paid source</span>
        </label>
        <label className="ops-check">
          <input
            type="checkbox"
            checked={notify}
            onChange={(event) => {
              setNotify(event.target.checked);
            }}
          />
          <span>Send the Telegram summary</span>
        </label>
      </div>
      <div className="ops-actions">
        <label className="ops-field">
          <span>Only one source</span>
          <select
            value={onlySource ?? ""}
            onChange={(event) => {
              setOnlySource(isSource(event.target.value) ? event.target.value : null);
            }}
          >
            <option value="">Every source</option>
            {Object.entries(SOURCE_LABEL).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label className="ops-field">
          <span>Apify count (empty: the configured count)</span>
          <input
            inputMode="numeric"
            value={apifyCount}
            onChange={(event) => {
              setApifyCount(event.target.value);
            }}
          />
        </label>
      </div>
      <div className="ops-actions">
        {UPLOADS.map((entry) => (
          <label key={entry.field} className="ops-field">
            <span>{entry.label}</span>
            <input
              type="file"
              accept=".json,.csv,application/json,text/csv"
              onChange={(event) => {
                const file = event.target.files?.[0];
                setUploads((current) => {
                  const next: Uploads = {};
                  for (const item of UPLOADS) {
                    const kept = item.field === entry.field ? file : current[item.field];
                    if (kept !== undefined) next[item.field] = kept;
                  }
                  return next;
                });
              }}
            />
          </label>
        ))}
      </div>
      <p className="ops-note">Each export may be up to 5 MB.</p>
      <div className="ops-actions">
        <button
          type="button"
          disabled={stream.active || start.isPending}
          onClick={() => {
            start.mutate();
          }}
        >
          Run discovery with these options
        </button>
      </div>
      {start.error !== null && (
        <p role="alert" className="ops-error">
          {start.error.message}
        </p>
      )}
    </section>
  );
}
