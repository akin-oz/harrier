import { useState } from "react";

import { DiscoveryEditor } from "./DiscoveryEditor";
import { HoldsEditor } from "./HoldsEditor";
import { ImportConfig } from "./ImportConfig";
import { LineListEditor } from "./LineListEditor";
import { useConfig } from "./useKindEditor";
import type { ConfigEntry } from "./useKindEditor";
import "./config.css";

/**
 * One editor per configuration kind, over the `/config` routes as they are
 * (spec 096), and the import button.
 *
 * A kind with no editor here is shown read-only rather than left out: the
 * academic searches are edited on the command line until an editor carries
 * spec 097's validation messages.
 */
export function ConfigSection() {
  const config = useConfig();
  // An import rewrites kinds behind every editor's back, so each editor
  // starts again from what the server then holds.
  const [generation, setGeneration] = useState(0);

  if (config.isPending) return <p className="config-muted">Reading the configuration…</p>;
  if (config.isError) {
    return (
      <p role="alert" className="config-kind__refusal">
        {config.error.message}
      </p>
    );
  }

  const byKind = new Map(config.data.map((entry) => [entry.kind, entry]));
  const known = new Set(["feeds", "linkedin_searches", "discovery", "company_holds"]);
  const others = config.data.filter((entry) => !known.has(entry.kind));
  const at = (kind: string): ConfigEntry | undefined => byKind.get(kind);
  const feeds = at("feeds");
  const searches = at("linkedin_searches");
  const discovery = at("discovery");
  const holds = at("company_holds");

  return (
    <div className="config-section">
      <ImportConfig
        onImported={() => {
          setGeneration((value) => value + 1);
        }}
      />
      {feeds && (
        <LineListEditor
          key={`feeds-${String(generation)}`}
          entry={feeds}
          title="Board watchlist"
          description="The job boards discovery reads: Greenhouse, Lever and Ashby board addresses."
        />
      )}
      {searches && (
        <LineListEditor
          key={`searches-${String(generation)}`}
          entry={searches}
          title="LinkedIn searches"
          description="The LinkedIn search addresses discovery runs through Apify."
        />
      )}
      {discovery && <DiscoveryEditor key={`discovery-${String(generation)}`} entry={discovery} />}
      {holds && <HoldsEditor key={`holds-${String(generation)}`} entry={holds} />}
      {others.map((entry) => (
        <ReadOnlyKind key={`${entry.kind}-${String(generation)}`} entry={entry} />
      ))}
    </div>
  );
}

function ReadOnlyKind({ entry }: { entry: ConfigEntry }) {
  return (
    <section className="config-kind" aria-label={entry.kind}>
      <div className="config-kind__head">
        <h3 className="config-kind__title">{entry.kind.replace(/_/g, " ")}</h3>
        <span className="config-source">
          {entry.source === "store" ? "From the store" : "From the file in the checkout"}
        </span>
      </div>
      <p className="config-kind__description">
        Read-only here. Change it with <code>harrier config set {entry.kind}</code>.
      </p>
      {entry.error !== null ? (
        <p role="alert" className="config-kind__refusal">
          The stored value cannot be read: {entry.error}
        </p>
      ) : (
        <pre className="config-readonly">{JSON.stringify(entry.value, null, 2)}</pre>
      )}
    </section>
  );
}
